"""
Strategy: CompletionGate — determines when a task is done.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("strategy.gate")


@dataclass
class GateDecision:
    """Result of a completion check."""
    met: bool
    reason: str
    current_items: int
    current_quality: float


class CompletionGate:
    """Check if crawl task has met its completion criteria.

    Evaluates against CrawlSpec's min_items and quality_threshold.
    Called by Governor to decide whether to nudge or stop.
    """

    def check(self, data: list[dict], spec) -> GateDecision:
        """Check if collected data meets spec requirements.

        Args:
            data: Extracted records so far.
            spec: CrawlSpec with min_items and quality_threshold.

        Returns:
            GateDecision indicating whether criteria are met.
        """
        count = len(data)
        min_items = getattr(spec, "min_items", 10)
        threshold = getattr(spec, "quality_threshold", 0.7)

        quality = self._estimate_quality(data, spec)

        if count >= min_items and quality >= threshold:
            return GateDecision(
                met=True,
                reason=f"Target met: {count}/{min_items} items, quality {quality:.1%}",
                current_items=count,
                current_quality=quality,
            )

        reasons = []
        if count < min_items:
            reasons.append(f"items {count}/{min_items}")
        if quality < threshold:
            reasons.append(f"quality {quality:.1%}/{threshold:.0%}")

        return GateDecision(
            met=False,
            reason=f"Not met: {', '.join(reasons)}",
            current_items=count,
            current_quality=quality,
        )

    def _estimate_quality(self, data: list[dict], spec) -> float:
        """Estimate data quality based on field completeness.

        Quality = average ratio of non-empty fields across all records.
        If spec has target_fields, only those fields count.
        """
        if not data:
            return 0.0

        target_fields = getattr(spec, "target_fields", None)
        if target_fields:
            field_names = [f["name"] for f in target_fields if isinstance(f, dict)]
        else:
            # Use all keys from first record
            field_names = list(data[0].keys()) if data else []

        if not field_names:
            return 1.0 if data else 0.0

        _EMPTY = {"n/a", "none", "null", "undefined", "", "unknown", "na"}

        total_score = 0.0
        for record in data:
            filled = sum(
                1 for f in field_names
                if str(record.get(f, "")).strip().lower() not in _EMPTY
            )
            total_score += filled / len(field_names)

        return total_score / len(data)
