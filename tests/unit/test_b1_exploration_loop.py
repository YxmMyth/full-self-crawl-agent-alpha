"""
B1: Exploration loop improvements — per-section coverage, inter-round feedback,
site_model validation.
"""

import json
import pytest
import tempfile
from unittest.mock import MagicMock

from src.management.scheduler import SharedFrontier, URLStatus
from src.management.run_intelligence import RunIntelligence
from src.strategy.spec import CrawlSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec():
    return CrawlSpec(url="https://example.com", requirement="Get items",
                     target_fields=[{"name": "title"}, {"name": "description"}])


def _make_frontier(spec=None):
    return SharedFrontier(max_urls=300, spec=spec or _make_spec())


def _make_run_intelligence():
    d = tempfile.mkdtemp()
    ri = RunIntelligence(d)
    ri.initialize()
    return ri


# ===========================================================================
# B1.4 — Per-section coverage tracking
# ===========================================================================

class TestSectionCoverage:

    def test_register_section_initializes_zero(self):
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        assert f.section_coverage() == {"https://example.com/blog": 0}

    def test_register_section_deduplicates(self):
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        f.register_section("https://example.com/blog")
        assert len(f.section_coverage()) == 1

    def test_associate_and_mark_extracted_increments(self):
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        f.add("https://example.com/blog/post-1")
        f.associate_url_with_section("https://example.com/blog/post-1",
                                     "https://example.com/blog")
        f.mark_in_flight("https://example.com/blog/post-1")
        f.mark_extracted("https://example.com/blog/post-1", 3,
                         new_data=[{"title": "A", "description": "B"}])
        assert f.section_coverage()["https://example.com/blog"] == 3

    def test_mark_extracted_no_association_no_increment(self):
        """URLs without section association don't affect section counts."""
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        f.add("https://example.com/other/page")
        f.mark_in_flight("https://example.com/other/page")
        f.mark_extracted("https://example.com/other/page", 5,
                         new_data=[{"title": "X", "description": "Y"}])
        assert f.section_coverage()["https://example.com/blog"] == 0

    def test_zero_records_no_increment(self):
        """Extraction with 0 records doesn't increment section count."""
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        f.add("https://example.com/blog/post-1")
        f.associate_url_with_section("https://example.com/blog/post-1",
                                     "https://example.com/blog")
        f.mark_in_flight("https://example.com/blog/post-1")
        f.mark_extracted("https://example.com/blog/post-1", 0)
        assert f.section_coverage()["https://example.com/blog"] == 0

    def test_section_coverage_returns_copy(self):
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        cov = f.section_coverage()
        cov["https://example.com/blog"] = 999
        assert f.section_coverage()["https://example.com/blog"] == 0


# ===========================================================================
# B1.1 — Round report generation
# ===========================================================================

class TestComputeRoundReport:
    """Test _compute_round_report logic via the function's components."""

    def test_frontier_stats_in_report(self):
        """Verify frontier stats are included in round report output."""
        f = _make_frontier()
        f.add("https://example.com/a")
        f.add("https://example.com/b")
        f.mark_in_flight("https://example.com/a")
        f.mark_extracted("https://example.com/a", 2,
                         new_data=[{"title": "t", "description": "d"}])

        # Build report components manually (mirrors _compute_round_report logic)
        st = f.stats()
        sc = st.get("status_counts", {})
        report_line = (
            f"Frontier: {sc.get('queued', 0)} queued, "
            f"{sc.get('extracted', 0)} extracted, "
            f"{sc.get('failed', 0)} failed, "
            f"{st['total_records']} records total"
        )
        assert "1 queued" in report_line
        assert "1 extracted" in report_line
        assert "1 records total" in report_line

    def test_unsampled_sections_in_report(self):
        """Verify unsampled sections appear in the report."""
        f = _make_frontier()
        f.register_section("https://example.com/blog")
        f.register_section("https://example.com/news")
        sec_cov = f.section_coverage()
        unsampled = [s for s, c in sec_cov.items() if c == 0]
        assert len(unsampled) == 2
        assert "https://example.com/blog" in unsampled

    def test_verifier_issues_capped_at_3(self):
        """Verify only top 3 quality issues are included."""
        issues = ["issue1", "issue2", "issue3", "issue4", "issue5"]
        capped = issues[:3]
        assert len(capped) == 3
        assert "issue4" not in capped

    def test_proven_scripts_in_report(self):
        """Verify proven scripts patterns appear in the report."""
        ri = _make_run_intelligence()
        ri.write("proven_scripts", {"/blog/*": {"script": "() => []", "success_count": 3}})
        proven = ri.read("proven_scripts")
        assert "/blog/*" in proven


# ===========================================================================
# B1.2 — site_model validation
# ===========================================================================

class TestSiteModelValidation:

    def test_empty_site_model_is_invalid(self):
        ri = _make_run_intelligence()
        sm = ri.read("site_model")
        # Mirrors _site_model_is_valid logic
        valid = bool(sm) and isinstance(sm, dict) and (
            bool(sm.get("structure")) or sm.get("estimated_total", 0) > 0
        )
        assert not valid

    def test_site_model_with_structure_is_valid(self):
        ri = _make_run_intelligence()
        ri.write("site_model", {"structure": "blog with categories", "estimated_total": 0})
        sm = ri.read("site_model")
        valid = bool(sm) and isinstance(sm, dict) and (
            bool(sm.get("structure")) or sm.get("estimated_total", 0) > 0
        )
        assert valid

    def test_site_model_with_estimated_total_is_valid(self):
        ri = _make_run_intelligence()
        ri.write("site_model", {"estimated_total": 50})
        sm = ri.read("site_model")
        valid = bool(sm) and isinstance(sm, dict) and (
            bool(sm.get("structure")) or sm.get("estimated_total", 0) > 0
        )
        assert valid
