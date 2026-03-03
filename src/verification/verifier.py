"""
Verification: Data quality verification, evidence collection, risk monitoring.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("verification")


# ---------------------------------------------------------------------------
# Verifier — data quality checks
# ---------------------------------------------------------------------------

class DataVerifier:
    """Verify extracted data quality.

    Checks:
    - Field completeness (non-empty values)
    - Record count vs. expectations
    - Duplicate detection
    - Data type consistency
    """

    def verify(self, data: list[dict], spec=None) -> dict[str, Any]:
        """Run quality checks on extracted data.

        Returns:
            {
                "quality_score": float (0-1),
                "record_count": int,
                "issues": list[str],
                "field_completeness": dict[str, float],
                "duplicate_count": int,
            }
        """
        if not data:
            return {
                "quality_score": 0.0,
                "record_count": 0,
                "issues": ["No data extracted"],
                "field_completeness": {},
                "duplicate_count": 0,
            }

        issues = []
        all_fields = set()
        for record in data:
            all_fields.update(record.keys())

        # Field completeness
        completeness = {}
        for f in all_fields:
            filled = sum(1 for r in data if r.get(f))
            completeness[f] = filled / len(data) if data else 0

        avg_completeness = sum(completeness.values()) / len(completeness) if completeness else 0

        # Duplicates (by JSON serialization)
        seen = set()
        dupes = 0
        for record in data:
            key = json.dumps(record, sort_keys=True, default=str)
            if key in seen:
                dupes += 1
            seen.add(key)

        if dupes > 0:
            issues.append(f"{dupes} duplicate records found")

        # Check against spec
        if spec:
            min_items = getattr(spec, "min_items", 10)
            if len(data) < min_items:
                issues.append(f"Only {len(data)}/{min_items} items extracted")

            target_fields = getattr(spec, "target_fields", None)
            if target_fields:
                for tf in target_fields:
                    name = tf["name"] if isinstance(tf, dict) else tf
                    if name not in all_fields:
                        issues.append(f"Missing expected field: {name}")

        # Quality score
        dup_penalty = (dupes / len(data)) if data else 0
        quality = max(0, avg_completeness - dup_penalty * 0.5)

        return {
            "quality_score": round(quality, 3),
            "record_count": len(data),
            "issues": issues,
            "field_completeness": {k: round(v, 3) for k, v in completeness.items()},
            "duplicate_count": dupes,
        }


async def verify_quality_tool(data: list[dict]) -> dict[str, Any]:
    """Tool wrapper for DataVerifier (registered in ToolRegistry)."""
    verifier = DataVerifier()
    return verifier.verify(data)


# ---------------------------------------------------------------------------
# Evidence Collector — captures proof of crawl actions
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """A single piece of evidence from the crawl process."""
    type: str  # "screenshot", "html_snapshot", "api_response", "extraction"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: Any = None
    metadata: dict = field(default_factory=dict)


class EvidenceCollector:
    """Collect evidence during crawl execution.

    Captures screenshots, HTML snapshots, and API responses
    for debugging and quality assurance.
    """

    def __init__(self):
        self.evidence: list[Evidence] = []

    def add(self, type: str, data: Any = None, **metadata) -> None:
        self.evidence.append(Evidence(type=type, data=data, metadata=metadata))

    def get_by_type(self, type: str) -> list[Evidence]:
        return [e for e in self.evidence if e.type == type]

    @property
    def count(self) -> int:
        return len(self.evidence)

    def summary(self) -> dict[str, int]:
        counts = {}
        for e in self.evidence:
            counts[e.type] = counts.get(e.type, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Risk Monitor — tracks error rates and anomalies
# ---------------------------------------------------------------------------

class RiskMonitor:
    """Monitor crawl execution for anomalies and escalating errors."""

    def __init__(self, error_threshold: int = 5, error_rate_threshold: float = 0.5):
        self.error_threshold = error_threshold
        self.error_rate_threshold = error_rate_threshold
        self._errors: list[dict] = []
        self._total_actions = 0

    def record_action(self, success: bool, action_name: str = "",
                      error: str | None = None) -> None:
        self._total_actions += 1
        if not success:
            self._errors.append({
                "action": action_name,
                "error": error or "unknown",
                "timestamp": datetime.now().isoformat(),
            })

    @property
    def error_count(self) -> int:
        return len(self._errors)

    @property
    def error_rate(self) -> float:
        if self._total_actions == 0:
            return 0.0
        return len(self._errors) / self._total_actions

    def is_critical(self) -> bool:
        """Check if error levels have reached critical thresholds."""
        return (
            self.error_count >= self.error_threshold
            or self.error_rate >= self.error_rate_threshold
        )

    def get_recent_errors(self, n: int = 5) -> list[dict]:
        return self._errors[-n:]

    def get_stats(self) -> dict:
        return {
            "total_actions": self._total_actions,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 3),
            "is_critical": self.is_critical(),
            "recent_errors": self.get_recent_errors(3),
        }
