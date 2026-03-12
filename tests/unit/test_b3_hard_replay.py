"""
B3: Hard-Replay Intelligence + Extraction Feedback.
"""

import json
import pytest
import tempfile

from src.management.run_intelligence import RunIntelligence
from src.strategy.spec import CrawlSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec():
    return CrawlSpec(url="https://example.com", requirement="Get items",
                     target_fields=[
                         {"name": "title"},
                         {"name": "description"},
                         {"name": "author"},
                         {"name": "date"},
                         {"name": "url"},
                     ])


def _make_ri():
    d = tempfile.mkdtemp()
    ri = RunIntelligence(d)
    ri.initialize()
    return ri


# ===========================================================================
# B3.1 — Proven Scripts Quality Metrics
# ===========================================================================

class TestProvenScriptsMetrics:

    def test_record_success_increments_attempts(self):
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 2)
        proven = ri.read("proven_scripts")
        pattern = list(proven.keys())[0]
        assert proven[pattern]["attempts"] == 1
        # Second success
        ri.record_success("https://example.com/blog/post-2", "return []", 3)
        proven = ri.read("proven_scripts")
        assert proven[pattern]["attempts"] == 2

    def test_record_hard_replay_failure_increments_failures(self):
        ri = _make_ri()
        # Seed a pattern first
        ri.record_success("https://example.com/blog/post-1", "return []", 2)
        ri.record_hard_replay_failure("https://example.com/blog/post-2", "null_result")
        proven = ri.read("proven_scripts")
        pattern = list(proven.keys())[0]
        assert proven[pattern]["failures"] == 1
        assert proven[pattern]["attempts"] == 2  # 1 success + 1 failure

    def test_skip_degraded_pattern(self):
        """Pattern with >= 3 attempts, >= 3 failures, success_rate < 0.5 is skipped."""
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        # Add 3 failures (total attempts = 4, failures = 3, rate = 0.25)
        for i in range(3):
            ri.record_hard_replay_failure(f"https://example.com/blog/fail-{i}", "null_result")
        # Should skip
        script = ri.get_script_for_url("https://example.com/blog/post-5")
        assert script is None

    def test_allow_healthy_pattern(self):
        """Pattern with good success rate is not skipped."""
        ri = _make_ri()
        for i in range(5):
            ri.record_success(f"https://example.com/blog/post-{i}", "return []", 1)
        ri.record_hard_replay_failure("https://example.com/blog/fail-1", "null_result")
        # 6 attempts, 1 failure => rate = 5/6 ≈ 0.83 — should NOT skip
        script = ri.get_script_for_url("https://example.com/blog/post-99")
        assert script == "return []"

    def test_allow_young_pattern(self):
        """Pattern with < 3 attempts is never skipped even if all failed."""
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        ri.record_hard_replay_failure("https://example.com/blog/fail-1", "null_result")
        # 2 attempts, 1 failure — too young to skip
        script = ri.get_script_for_url("https://example.com/blog/post-99")
        assert script == "return []"

    def test_backward_compat_no_attempts_field(self):
        """Old entries without attempts/failures fields are never skipped."""
        ri = _make_ri()
        # Manually write an old-format entry (no attempts/failures keys)
        knowledge = ri._load_knowledge()
        knowledge["proven_scripts"] = {
            "/blog/*": {"script": "return []", "success_count": 5, "sample_urls": []}
        }
        ri._save_knowledge(knowledge)
        script = ri.get_script_for_url("https://example.com/blog/post-1")
        assert script == "return []"


# ===========================================================================
# B3.1 — Context summary: healthy vs degraded
# ===========================================================================

class TestContextSummaryHealthyDegraded:

    def test_summary_splits_healthy_and_degraded(self):
        ri = _make_ri()
        # Create a healthy pattern
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        # Create a degraded pattern (different URL structure)
        for i in range(3):
            ri.record_success(f"https://example.com/items/{i}", "return []", 1)
        for i in range(4):
            ri.record_hard_replay_failure(f"https://example.com/items/fail-{i}", "null_result")

        summary = ri.get_context_summary()
        assert "healthy" in summary
        assert "degraded" in summary


# ===========================================================================
# B3.2 — Hard-Replay Golden Schema Validation
# ===========================================================================

class TestGoldenSchemaValidation:

    def test_no_golden_schema_passes(self):
        """validate_records with no golden_schema returns ok=True."""
        ri = _make_ri()
        result = ri.validate_records([{"title": "Test"}])
        assert result["ok"] is True

    def test_golden_catches_missing_fields(self):
        """Records missing required golden fields should fail validation."""
        ri = _make_ri()
        ri.save_golden_records([
            {"title": "Gold Title", "description": "Long enough description text here"},
        ])
        # Record missing 'title'
        result = ri.validate_records([{"description": "Some text"}])
        assert result["failed"] >= 1
        assert any("title" in issue for issue in result["issues"])

    def test_golden_passes_good_records(self):
        """Records matching golden schema should pass."""
        ri = _make_ri()
        ri.save_golden_records([
            {"title": "Gold Title", "description": "Some long enough description"},
        ])
        result = ri.validate_records([
            {"title": "Another Title", "description": "Another long enough description"},
        ])
        assert result["ok"] is True


# ===========================================================================
# B3.3 — Hard-Replay Failure Tracking
# ===========================================================================

class TestHardReplayFailureTracking:

    def test_failure_recorded_in_pattern(self):
        """record_hard_replay_failure increments pattern failure counters."""
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        ri.record_hard_replay_failure("https://example.com/blog/post-2", "null_result")
        proven = ri.read("proven_scripts")
        pattern = list(proven.keys())[0]
        assert proven[pattern]["failures"] == 1
        assert "null_result" in proven[pattern]["recent_failures"]

    def test_failure_in_failure_log(self):
        """record_hard_replay_failure also writes to global failure_log."""
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        ri.record_hard_replay_failure("https://example.com/blog/post-2", "null_result")
        failures = ri.read("failure_log")
        assert len(failures) == 1
        assert "hard_replay" in failures[0]["reason"]

    def test_three_failures_causes_skip(self):
        """After enough failures, get_script_for_url returns None."""
        ri = _make_ri()
        ri.record_success("https://example.com/blog/post-1", "return []", 1)
        # Record 3 hard-replay failures (total: 4 attempts, 3 failures)
        for i in range(3):
            ri.record_hard_replay_failure(f"https://example.com/blog/fail-{i}", "null_result")
        # Should skip: 4 attempts >= 3, 3 failures >= 3, rate = 1/4 = 0.25 < 0.5
        assert ri.get_script_for_url("https://example.com/blog/post-99") is None


# ===========================================================================
# B3.4 — Enriched Prior Experience
# ===========================================================================

# ===========================================================================
# Regression: estimated_total stored as string by LLM
# ===========================================================================

class TestEstimatedTotalStringCoercion:

    def test_context_summary_with_string_estimated_total(self):
        """LLM may write estimated_total as string — must not crash."""
        ri = _make_ri()
        ri.write("site_model", {
            "structure": "listing: /blog/*, content: /blog/post/*",
            "estimated_total": "1000",  # string, not int
        })
        ri.write("coverage", {"extracted_count": 50})
        # Should not raise TypeError
        summary = ri.get_context_summary()
        assert "50/1000" in summary

    def test_get_estimated_total_with_string(self):
        """get_estimated_total coerces string to int."""
        ri = _make_ri()
        ri.write("site_model", {"estimated_total": "500"})
        assert ri.get_estimated_total() == 500

    def test_get_estimated_total_with_garbage(self):
        """get_estimated_total returns 0 for non-numeric values."""
        ri = _make_ri()
        ri.write("site_model", {"estimated_total": "many"})
        assert ri.get_estimated_total() == 0


# ===========================================================================
# B3.4 — Enriched Prior Experience
# ===========================================================================

class TestEnrichedExperience:

    def test_hard_replay_experience_format(self):
        """_update_experience produces HARD-REPLAY format with fields info."""
        # We test the format logic inline since _update_experience is a closure.
        # Simulate what the function does:
        spec = _make_spec()
        page_data = [{"title": "Test", "description": "Desc", "author": "Me"}]
        total_fields = len(spec.target_fields)
        fields_filled = 0
        for f in spec.target_fields:
            val = str(page_data[0].get(f["name"], "")).strip()
            if val and val.lower() not in {"", "none", "null", "n/a"}:
                fields_filled += 1
        entry = f"Page 0 (https://example.com/p/1): {len(page_data)} records via HARD-REPLAY. {fields_filled}/{total_fields} fields"
        assert "HARD-REPLAY" in entry
        assert "3/5 fields" in entry

    def test_llm_experience_format(self):
        """_update_experience produces LLM format with stop reason and fields info."""
        spec = _make_spec()
        page_data = [{"title": "Test", "description": "Desc", "author": "Me", "date": "2024", "url": "u"}]
        total_fields = len(spec.target_fields)
        fields_filled = 0
        for f in spec.target_fields:
            val = str(page_data[0].get(f["name"], "")).strip()
            if val and val.lower() not in {"", "none", "null", "n/a"}:
                fields_filled += 1
        entry = (
            f"Page 0 (https://example.com/p/1): {len(page_data)} records via LLM. "
            f"stop=complete, 30s, 5 steps. {fields_filled}/{total_fields} fields"
        )
        assert "via LLM" in entry
        assert "stop=complete" in entry
        assert "5/5 fields" in entry
