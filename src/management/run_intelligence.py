"""
Management: RunIntelligence — run-level knowledge accumulation.

Persists across all agent sessions within a single crawl run.
Inspired by Manus's file-centric memory architecture and Anthropic's
structured note-taking pattern for long-running agents.

Three responsibilities:
1. run_knowledge.json  — all agents read/write (proven scripts, coverage, site model)
2. golden_records.json — Explorer writes, Extractors use for structural validation

The key insight: agents should not rely on LLM context for cross-session knowledge.
Files are the source of truth. Context is ephemeral. Files accumulate.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..utils.url import normalize_url

logger = logging.getLogger("management.run_intelligence")


class RunIntelligence:
    """Run-level shared knowledge store.

    Created once per crawl run by the Orchestrator.
    Explorer writes the initial site model and golden records.
    Each Extractor reads proven strategies and writes back what it learns.
    Coverage and failure state accumulate across all sessions.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self._knowledge_file = self.base_dir / "run_knowledge.json"
        self._golden_file = self.base_dir / "golden_records.json"

    def initialize(self) -> None:
        """Create empty knowledge files at run start."""
        self.base_dir.mkdir(parents=True, exist_ok=True)

        empty_knowledge = {
            "site_model": {},
            "proven_scripts": {},
            "failure_log": [],
            "coverage": {"estimated_total": 0, "extracted_count": 0},
            "replan_triggers": [],
        }
        with open(self._knowledge_file, "w", encoding="utf-8") as f:
            json.dump(empty_knowledge, f, indent=2, ensure_ascii=False)

        with open(self._golden_file, "w", encoding="utf-8") as f:
            json.dump({"records": [], "schema": {}}, f, indent=2, ensure_ascii=False)

        logger.info(f"RunIntelligence initialized at {self.base_dir}")

    # ------------------------------------------------------------------
    # Generic read/write (agent tools)
    # ------------------------------------------------------------------

    def _load_knowledge(self) -> dict:
        if not self._knowledge_file.exists():
            return {}
        try:
            with open(self._knowledge_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_knowledge(self, data: dict) -> None:
        self._atomic_write(self._knowledge_file, data)

    def read(self, key: str | None = None) -> dict:
        """Read from run_knowledge. key=None returns everything."""
        knowledge = self._load_knowledge()
        if key is None:
            return knowledge
        return knowledge.get(key, {})

    def write(self, key: str, value: Any) -> dict:
        """Write a value to run_knowledge atomically.

        Supports dot notation for nested keys: "proven_scripts.*/pen/*"
        """
        knowledge = self._load_knowledge()

        if "." in key:
            parent, child = key.split(".", 1)
            if parent not in knowledge or not isinstance(knowledge[parent], dict):
                knowledge[parent] = {}
            knowledge[parent][child] = value
        elif key == "proven_scripts" and isinstance(value, dict):
            # proven_scripts is accumulated — merge patterns, never replace.
            # LLM writes individual patterns; record_success() also writes here.
            existing = knowledge.get("proven_scripts", {})
            if isinstance(existing, dict):
                existing.update(value)
                knowledge["proven_scripts"] = existing
            else:
                knowledge["proven_scripts"] = value
        else:
            knowledge[key] = value

        self._save_knowledge(knowledge)
        logger.debug(f"RunIntelligence.write: {key}")
        return {"ok": True, "key": key}

    # ------------------------------------------------------------------
    # Golden records
    # ------------------------------------------------------------------

    def _atomic_write(self, filepath: Path, data: dict) -> None:
        """Write JSON atomically: temp file → os.replace. Crash-safe."""
        parent = str(filepath.parent)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_path, str(filepath))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def save_golden_records(self, records: list[dict]) -> None:
        """Explorer saves 1-3 verified samples as structural reference."""
        if not records:
            return
        schema = self._infer_schema(records)
        data = {"records": records[:3], "schema": schema}
        self._atomic_write(self._golden_file, data)
        logger.info(f"RunIntelligence: saved {min(len(records), 3)} golden records")

    def get_golden_records(self) -> list[dict]:
        if not self._golden_file.exists():
            return []
        try:
            with open(self._golden_file, "r", encoding="utf-8") as f:
                return json.load(f).get("records", [])
        except (json.JSONDecodeError, IOError):
            return []

    def get_golden_schema(self) -> dict:
        if not self._golden_file.exists():
            return {}
        try:
            with open(self._golden_file, "r", encoding="utf-8") as f:
                return json.load(f).get("schema", {})
        except (json.JSONDecodeError, IOError):
            return {}

    # ------------------------------------------------------------------
    # Structural validation (non-LLM, deterministic)
    # ------------------------------------------------------------------

    def validate_records(self, records: list[dict], page_html: str = "") -> dict:
        """Validate extracted records against golden schema.

        Three checks (all deterministic, no LLM needed):
        1. Required fields present?
        2. Long fields not suspiciously short (selector mismatch detection)?
        3. Key field values appear in page HTML (hallucination guard)?

        Returns {"ok": bool, "issues": [str], "passed": N, "failed": N}
        """
        schema = self.get_golden_schema()
        if not schema or not records:
            return {"ok": True, "issues": [], "passed": len(records), "failed": 0}

        required_fields = [f for f, s in schema.items() if s.get("required")]
        issues: list[str] = []
        passed = failed = 0

        for record in records:
            rec_issues = []

            # 1. Required fields
            for field in required_fields:
                val = str(record.get(field, "")).strip().lower()
                if not val or val in {"null", "none", "undefined", "n/a", ""}:
                    rec_issues.append(f"missing required field '{field}'")

            # 2. Length sanity (catch wrong-selector extractions)
            for field, fschema in schema.items():
                min_len = fschema.get("min_length", 0)
                val = str(record.get(field, ""))
                if min_len > 20 and len(val) < min_len * 0.1:
                    rec_issues.append(
                        f"'{field}' suspiciously short: {len(val)} chars "
                        f"(golden avg {fschema.get('avg_length', '?')} chars)"
                    )

            # 3. Content anchoring: title/name must appear in page HTML
            if page_html:
                for anchor_field in ("title", "name", "headline", "author"):
                    val = str(record.get(anchor_field, "")).strip()
                    if val and len(val) > 5 and val not in page_html:
                        rec_issues.append(
                            f"'{anchor_field}'='{val[:40]}' not found in page HTML — "
                            f"possible hallucination or wrong selector"
                        )
                        break  # One anchor failure is enough to flag

            if rec_issues:
                failed += 1
                issues.extend(rec_issues[:2])
            else:
                passed += 1

        ok = (failed == 0) or (passed / len(records) >= 0.8)
        return {"ok": ok, "issues": issues[:6], "passed": passed, "failed": failed}

    # ------------------------------------------------------------------
    # Extraction outcome tracking
    # ------------------------------------------------------------------

    def record_success(self, url: str, script: str, records_count: int) -> None:
        """Record a successful extraction pattern."""
        knowledge = self._load_knowledge()
        proven = knowledge.setdefault("proven_scripts", {})

        pattern = self._url_to_pattern(url)
        entry = proven.setdefault(pattern, {"success_count": 0, "sample_urls": []})
        entry["script"] = script
        entry["success_count"] = entry.get("success_count", 0) + records_count
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["sample_urls"] = (entry.get("sample_urls", []) + [url])[-3:]

        coverage = knowledge.setdefault("coverage", {})
        coverage["extracted_count"] = coverage.get("extracted_count", 0) + records_count

        self._save_knowledge(knowledge)

    def _find_matching_pattern(self, url: str, proven: dict) -> str | None:
        """Find the proven_scripts pattern key that matches a URL.

        Returns the pattern string or None. Extracted from get_script_for_url
        so both lookup and failure-tracking use the same match logic.
        """
        norm = normalize_url(url)
        if norm in proven:
            return norm
        for pattern in proven:
            if self._url_matches_pattern(norm, pattern):
                return pattern
        return None

    def record_hard_replay_failure(self, url: str, reason: str) -> None:
        """Record a hard-replay failure against the matching proven pattern.

        Increments attempts + failures, appends to recent_failures[-3:],
        and delegates to record_failure for the global failure_log.
        """
        knowledge = self._load_knowledge()
        proven = knowledge.get("proven_scripts", {})
        pattern = self._find_matching_pattern(url, proven)
        if pattern and pattern in proven:
            entry = proven[pattern]
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["failures"] = entry.get("failures", 0) + 1
            recent = entry.get("recent_failures", [])
            recent.append(reason)
            entry["recent_failures"] = recent[-3:]
            self._save_knowledge(knowledge)
        self.record_failure(url, f"hard_replay: {reason}")

    def record_failure(self, url: str, reason: str) -> None:
        """Record a failure for the replan trigger system."""
        knowledge = self._load_knowledge()

        triggers = knowledge.setdefault("replan_triggers", [])
        triggers.append({"url": url, "reason": reason})
        knowledge["replan_triggers"] = triggers[-20:]

        failures = knowledge.setdefault("failure_log", [])
        failures.append({"url": url, "reason": reason})
        knowledge["failure_log"] = failures[-50:]

        self._save_knowledge(knowledge)

    def needs_replan(self) -> bool:
        """Return True if enough failures accumulated to warrant a partial replan."""
        knowledge = self._load_knowledge()
        return len(knowledge.get("replan_triggers", [])) >= 3

    def clear_replan_triggers(self) -> None:
        knowledge = self._load_knowledge()
        knowledge["replan_triggers"] = []
        self._save_knowledge(knowledge)

    # ------------------------------------------------------------------
    # Coverage / completion
    # ------------------------------------------------------------------

    def get_estimated_total(self) -> int:
        knowledge = self._load_knowledge()
        raw = knowledge.get("site_model", {}).get("estimated_total", 0)
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 0

    def update_coverage(self, extracted_count: int) -> None:
        knowledge = self._load_knowledge()
        coverage = knowledge.setdefault("coverage", {})
        coverage["extracted_count"] = extracted_count
        self._save_knowledge(knowledge)

    # ------------------------------------------------------------------
    # Context injection helpers
    # ------------------------------------------------------------------

    def get_context_summary(self) -> str:
        """Compact, structured summary for injection into each agent's task context."""
        knowledge = self._load_knowledge()
        lines = []

        site_model = knowledge.get("site_model", {})
        if site_model:
            lines.append("## Accumulated Run Knowledge")
            if site_model.get("structure"):
                lines.append(f"Site structure: {site_model['structure']}")
            est = site_model.get("estimated_total", 0)
            if est:
                basis = site_model.get("estimation_basis", "")
                lines.append(f"Estimated total items: ~{est}" + (f" ({basis})" if basis else ""))
            if site_model.get("content_url_pattern"):
                lines.append(f"Content URL pattern: {site_model['content_url_pattern']}")
            if site_model.get("extraction_hint"):
                lines.append(f"Extraction hint: {site_model['extraction_hint']}")

        proven = knowledge.get("proven_scripts", {})
        if proven:
            healthy = []
            degraded = []
            for pat, entry in proven.items():
                attempts = entry.get("attempts", 0)
                failures = entry.get("failures", 0)
                if attempts >= 3 and failures >= 3 and (attempts - failures) / max(attempts, 1) < 0.5:
                    degraded.append(pat)
                else:
                    healthy.append(pat)
            if healthy:
                lines.append(f"Proven extraction patterns (healthy): {healthy}")
                lines.append("→ Call read_run_knowledge('proven_scripts') for the verified scripts")
            if degraded:
                lines.append(f"Proven extraction patterns (degraded, will skip): {degraded}")

        failures = knowledge.get("failure_log", [])
        if failures:
            recent = list({f["url"] for f in failures[-5:]})
            lines.append(f"Known failures (skip these): {recent}")

        coverage = knowledge.get("coverage", {})
        extracted = coverage.get("extracted_count", 0)
        if extracted:
            try:
                total = int(site_model.get("estimated_total", 0))
            except (ValueError, TypeError):
                total = 0
            if total:
                lines.append(f"Coverage: {extracted}/{total} ({extracted/total:.0%}) extracted so far")
            else:
                lines.append(f"Extracted so far this run: {extracted} records")

        return "\n".join(lines) if lines else ""

    def get_golden_summary(self) -> str:
        """One-line golden record summary for context injection."""
        records = self.get_golden_records()
        if not records:
            return ""
        schema = self.get_golden_schema()
        fields = list(schema.keys()) if schema else list(records[0].keys())
        sample = {k: str(v)[:60] for k, v in records[0].items() if k in fields[:5]}
        return f"Golden record (Explorer-verified): {json.dumps(sample, ensure_ascii=False)}"

    def get_script_for_url(self, url: str) -> str | None:
        """Look up a proven extraction script for a URL.

        Skips degraded patterns: attempts >= 3 AND failures >= 3 AND success_rate < 0.5.
        Backward-compat: entries without attempts/failures fields are never skipped.
        """
        knowledge = self._load_knowledge()
        proven = knowledge.get("proven_scripts", {})
        pattern = self._find_matching_pattern(url, proven)
        if pattern is None:
            return None
        entry = proven[pattern]
        # Skip degraded patterns
        attempts = entry.get("attempts", 0)
        failures = entry.get("failures", 0)
        if attempts >= 3 and failures >= 3:
            success_rate = (attempts - failures) / attempts if attempts else 0
            if success_rate < 0.5:
                logger.info(
                    f"Skipping degraded proven script for {pattern}: "
                    f"{failures}/{attempts} failures, rate={success_rate:.0%}"
                )
                return None
        return entry.get("script")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _infer_schema(self, records: list[dict]) -> dict:
        """Infer field schema from a set of records."""
        if not records:
            return {}
        all_keys: set[str] = set()
        for r in records:
            all_keys.update(r.keys())

        schema = {}
        for key in all_keys:
            values = [str(r.get(key, "")) for r in records if r.get(key)]
            if not values:
                continue
            avg_len = sum(len(v) for v in values) / len(values)
            fill_ratio = len(values) / len(records)
            schema[key] = {
                "type": "string",
                "min_length": max(0, int(avg_len * 0.1)),
                "avg_length": int(avg_len),
                "required": fill_ratio >= 0.8,
            }
        return schema

    def _url_to_pattern(self, url: str) -> str:
        """Convert a specific URL to a reusable wildcard pattern.

        Returns anchored path pattern like /*/pen/* (leading /).
        Slug-like segments (3-16 alphanum chars) become wildcards.
        """
        from urllib.parse import urlparse
        path = urlparse(url).path
        segments = [s for s in path.split("/") if s]
        normalized = []
        for seg in segments:
            # Slug-like segments → wildcard
            if re.match(r'^[a-zA-Z0-9_-]{3,16}$', seg) and not seg.isdigit():
                normalized.append("*")
            else:
                normalized.append(seg)
        return "/" + "/".join(normalized) if normalized else "/*"

    def _url_matches_pattern(self, url: str, pattern: str) -> bool:
        """Check if a URL path matches a wildcard pattern with full-path anchoring.

        Pattern format: /segment/*/segment/* where * matches exactly one path segment.
        Both start (^) and end ($) are anchored — no substring matching.
        """
        from urllib.parse import urlparse
        path = urlparse(url).path.rstrip("/")
        segments = pattern.strip("/").split("/")
        regex_parts = []
        for seg in segments:
            if seg == "*":
                regex_parts.append("[^/]+")
            else:
                regex_parts.append(re.escape(seg))
        regex = "^/" + "/".join(regex_parts) + "$"
        try:
            return bool(re.match(regex, path))
        except re.error:
            return False
