"""
Management: Governor — LLM behavior governance.

The Governor doesn't choose strategies or make decisions for the LLM.
It monitors behavior and intervenes only when things go wrong:
- Budget exhaustion → force stop
- Loop detection → nudge to try different approach
- Time limit → force stop
- Completion gate passed → nudge to finish
"""

import logging
import time
from typing import Any

logger = logging.getLogger("management.governor")


class Governor:
    """LLM behavior governance engine.

    Intervention modes:
    - Nudge (soft): Inject text into system message to guide LLM
    - Force stop (hard): should_stop() returns reason, loop terminates

    Merged from old: MetaController monitoring + RiskMonitor + Pipeline error limits.
    """

    def __init__(
        self,
        max_steps: int = 30,
        max_llm_calls: int = 20,
        max_time_seconds: int = 300,
        gate=None,
        monitor=None,
    ):
        self.max_steps = max_steps
        self.max_llm_calls = max_llm_calls
        self.max_time_seconds = max_time_seconds
        self.gate = gate  # CompletionGate
        self.monitor = monitor  # RiskMonitor

        self._llm_calls = 0
        self._total_tokens = 0
        self._start_time: float | None = None

        # Navigate loop tracking (URL → visit count)
        self._navigate_counts: dict[str, int] = {}
        # Action hash window for repetition detection (like browser-use)
        self._recent_action_hashes: list[str] = []
        self._action_window_size = 15

    def start(self) -> None:
        """Mark the start of execution."""
        self._start_time = time.time()
        self._llm_calls = 0
        self._total_tokens = 0
        self._navigate_counts.clear()
        self._recent_action_hashes.clear()

    def record_llm_call(self, tokens: int = 0) -> None:
        """Record an LLM API call."""
        self._llm_calls += 1
        self._total_tokens += tokens

    def record_action(self, tool_name: str, args: dict) -> None:
        """Record an action for loop detection. Call after each tool execution."""
        import hashlib, json
        # Normalize action for hashing
        if tool_name == "navigate":
            key = f"navigate|{args.get('url', '')}"
            url = args.get("url", "")
            self._navigate_counts[url] = self._navigate_counts.get(url, 0) + 1
        elif tool_name == "think":
            return  # Don't track think calls for loop detection
        else:
            filtered = {k: v for k, v in sorted(args.items()) if v is not None}
            key = f"{tool_name}|{json.dumps(filtered, sort_keys=True, default=str)}"
        h = hashlib.sha256(key.encode()).hexdigest()[:12]
        self._recent_action_hashes.append(h)
        if len(self._recent_action_hashes) > self._action_window_size:
            self._recent_action_hashes = self._recent_action_hashes[-self._action_window_size:]

    def should_stop(self, history) -> str | None:
        """Check if execution should be force-stopped.

        Returns:
            Reason string if should stop, None if should continue.
        """
        # Step limit
        if history.count >= self.max_steps:
            return f"Step limit reached ({self.max_steps})"

        # LLM call limit
        if self._llm_calls >= self.max_llm_calls:
            return f"LLM call limit reached ({self.max_llm_calls})"

        # Time limit
        if self._start_time:
            elapsed = time.time() - self._start_time
            if elapsed >= self.max_time_seconds:
                return f"Time limit reached ({self.max_time_seconds}s)"

        # Critical error rate
        if self.monitor and self.monitor.is_critical():
            stats = self.monitor.get_stats()
            return f"Critical error rate: {stats['error_rate']:.0%} ({stats['error_count']} errors)"

        # Loop detection (same tool+args 3 times in a row, all failed)
        if history.count >= 3 and history.last_n_same_tool(3):
            recent = history.recent(3)
            if all(not s.succeeded for s in recent):
                return f"Stuck in loop: {recent[0].tool_name} failed 3 consecutive times with same args"

        # Navigate URL loop hard stop — same URL visited 5+ times means SPA isn't rendering
        # (rate-limited, blocked, or fundamentally wrong approach). Soft nudges at 2-4 didn't help.
        for url, count in self._navigate_counts.items():
            if count >= 5:
                return (
                    f"Navigation loop: '{url}' visited {count} times with no progress. "
                    f"SPA may be rate-limited or blocked. Stopping exploration."
                )

        return None

    def get_nudges(self, history, data: list[dict] | None = None,
                   spec=None) -> str | None:
        """Generate governance nudges for the LLM.

        Returns text to inject into system message, or None.
        """
        nudges = []

        # Budget warning (>70%)
        if self._llm_calls > 0:
            usage_pct = self._llm_calls / self.max_llm_calls
            if usage_pct >= 0.8:
                nudges.append(
                    f"⚠️ Budget: {self._llm_calls}/{self.max_llm_calls} LLM calls used "
                    f"({usage_pct:.0%}). STOP exploring and call report_urls() NOW with all URLs found so far."
                )
            elif usage_pct >= 0.7:
                nudges.append(
                    f"⚠️ Budget: {self._llm_calls}/{self.max_llm_calls} LLM calls used "
                    f"({usage_pct:.0%}). Wrap up: call report_urls() or js_extract_save() soon."
                )

        # Time warning (>70%)
        if self._start_time:
            elapsed = time.time() - self._start_time
            time_pct = elapsed / self.max_time_seconds
            if time_pct >= 0.7:
                mins = int(elapsed / 60)
                max_mins = int(self.max_time_seconds / 60)
                nudges.append(
                    f"⚠️ Time: {mins}min/{max_mins}min elapsed. "
                    f"Prioritize saving current results."
                )

        # Loop detection (soft — same tool 2 times, not necessarily failed)
        if history.count >= 2 and history.last_n_same_tool(2):
            recent = history.recent(2)
            tool = recent[0].tool_name
            if not recent[-1].succeeded:
                nudges.append(
                    f"⚠️ {tool} failed twice with same args. "
                    f"Try a different approach (execute_code, different selectors, etc.)."
                )

        # Navigate URL loop detection (escalating nudges)
        for url, count in self._navigate_counts.items():
            if count >= 4:
                nudges.append(
                    f"⚠️ You have navigated to {url} {count} times. "
                    f"This page is not delivering the content you need via navigation. "
                    f"Try: evaluate_js(\"Array.from(document.querySelectorAll('a[href]')).map(a=>a.href)\") "
                    f"or bash(curl) or a completely different URL."
                )
            elif count >= 3:
                nudges.append(
                    f"⚠️ You have navigated to {url} {count} times. "
                    f"Repeating the same navigation will not help. "
                    f"Call analyze_links() or evaluate_js() to read the current DOM instead."
                )
            elif count >= 2:
                nudges.append(
                    f"⚠️ Navigated to {url} twice with no progress. "
                    f"Before navigating again: call analyze_links() to check what the rendered DOM currently has."
                )

        # Action repetition detection (rolling window)
        if len(self._recent_action_hashes) >= 5:
            from collections import Counter
            counts = Counter(self._recent_action_hashes)
            most_common_hash, most_common_count = counts.most_common(1)[0]
            if most_common_count >= 5:
                nudges.append(
                    f"⚠️ You have repeated a similar action {most_common_count} times "
                    f"in the last {len(self._recent_action_hashes)} actions. "
                    f"If you're not making progress, try a different strategy."
                )

        # Completion gate
        if self.gate and data is not None and spec:
            decision = self.gate.check(data, spec)
            if decision.met:
                nudges.append(
                    f"✅ Completion criteria met: {decision.reason}. "
                    f"You may finish now."
                )

        # Error rate warning
        if self.monitor:
            stats = self.monitor.get_stats()
            if stats["error_rate"] >= 0.3 and stats["total_actions"] >= 3:
                nudges.append(
                    f"⚠️ High error rate: {stats['error_rate']:.0%}. "
                    f"Consider changing strategy."
                )

        return "\n".join(nudges) if nudges else None

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    def get_stats(self) -> dict:
        return {
            "llm_calls": self._llm_calls,
            "total_tokens": self._total_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "max_steps": self.max_steps,
            "max_llm_calls": self.max_llm_calls,
            "max_time_seconds": self.max_time_seconds,
        }
