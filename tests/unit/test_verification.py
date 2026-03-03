"""
Unit tests for verification layer: DataVerifier, EvidenceCollector, RiskMonitor.
"""

import pytest
from src.verification.verifier import DataVerifier, EvidenceCollector, RiskMonitor


class TestDataVerifier:
    def test_perfect_quality(self):
        v = DataVerifier()
        result = v.verify([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        assert result["quality_score"] == 1.0
        assert result["record_count"] == 2
        assert result["duplicate_count"] == 0
        assert result["issues"] == []

    def test_duplicate_detection(self):
        v = DataVerifier()
        result = v.verify([{"a": 1}, {"a": 1}, {"a": 2}])
        assert result["duplicate_count"] == 1
        assert any("duplicate" in i for i in result["issues"])

    def test_empty_data(self):
        v = DataVerifier()
        result = v.verify([])
        assert result["quality_score"] == 0.0
        assert "No data" in result["issues"][0]

    def test_spec_field_check(self):
        v = DataVerifier()
        from src.strategy.spec import CrawlSpec
        spec = CrawlSpec(url="x", requirement="y",
                         target_fields=[{"name": "title"}, {"name": "price"}])
        result = v.verify([{"title": "Book A"}], spec)
        assert any("price" in i for i in result["issues"])

    def test_partial_completeness(self):
        v = DataVerifier()
        result = v.verify([{"a": 1, "b": None}, {"a": 2, "b": 3}])
        assert 0 < result["quality_score"] < 1.0


class TestEvidenceCollector:
    def test_add_and_retrieve(self):
        ec = EvidenceCollector()
        ec.add("screenshot", data=b"img", page="test.html")
        ec.add("html_snapshot", data="<html>")
        assert ec.count == 2
        assert len(ec.get_by_type("screenshot")) == 1
        assert ec.summary() == {"screenshot": 1, "html_snapshot": 1}


class TestRiskMonitor:
    def test_no_errors(self):
        rm = RiskMonitor()
        rm.record_action(True, "navigate")
        rm.record_action(True, "click")
        assert rm.error_count == 0
        assert rm.error_rate == 0.0
        assert not rm.is_critical()

    def test_critical_threshold(self):
        rm = RiskMonitor(error_threshold=3)
        rm.record_action(False, "click", "not found")
        rm.record_action(False, "click", "timeout")
        rm.record_action(False, "fill", "error")
        assert rm.is_critical()
        assert rm.error_count == 3
        assert rm.error_rate == 1.0

    def test_error_rate_threshold(self):
        rm = RiskMonitor(error_rate_threshold=0.5)
        # Below min sample size (5) — should NOT be critical even at 50% error rate
        rm.record_action(True, "a")
        rm.record_action(False, "b", "err")
        assert rm.error_rate == 0.5
        assert not rm.is_critical()  # only 2 actions, need >=5
        # Add more actions to reach min sample
        rm.record_action(False, "c", "err")
        rm.record_action(False, "d", "err")
        rm.record_action(True, "e")
        # Now 5 actions, 3/5 = 60% error rate >= 50% threshold
        assert rm.is_critical()

    def test_get_recent_errors(self):
        rm = RiskMonitor()
        rm.record_action(False, "a", "err1")
        rm.record_action(False, "b", "err2")
        recent = rm.get_recent_errors(1)
        assert len(recent) == 1
        assert recent[0]["error"] == "err2"
