"""Unit tests for SharedFrontier state machine."""

import pytest
from src.management.scheduler import (
    SharedFrontier,
    URLStatus,
    URLRecord,
    QualitySignals,
)


class TestSharedFrontierAdd:
    def test_add_returns_true_for_new_url(self):
        f = SharedFrontier()
        assert f.add("https://example.com/a") is True

    def test_add_returns_false_for_duplicate(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        assert f.add("https://example.com/a") is False

    def test_add_normalizes_fragment(self):
        f = SharedFrontier()
        f.add("https://example.com/a#section")
        assert f.add("https://example.com/a") is False

    def test_add_normalizes_trailing_slash(self):
        f = SharedFrontier()
        f.add("https://example.com/a/")
        assert f.add("https://example.com/a") is False

    def test_add_respects_max_urls(self):
        f = SharedFrontier(max_urls=2)
        assert f.add("https://example.com/a") is True
        assert f.add("https://example.com/b") is True
        assert f.add("https://example.com/c") is False

    def test_add_batch(self):
        f = SharedFrontier()
        added = f.add_batch(["https://example.com/a", "https://example.com/b"])
        assert added == 2

    def test_add_batch_dict_items(self):
        f = SharedFrontier()
        added = f.add_batch([
            {"url": "https://example.com/a", "priority": 2.5},
            {"url": "https://example.com/b"},
        ])
        assert added == 2


class TestSharedFrontierNext:
    def test_next_returns_highest_priority(self):
        f = SharedFrontier()
        f.add("https://example.com/low", priority=1.0)
        f.add("https://example.com/high", priority=3.0)
        f.add("https://example.com/mid", priority=2.0)
        record = f.next()
        assert record is not None
        assert record.url == "https://example.com/high"

    def test_next_returns_none_when_empty(self):
        f = SharedFrontier()
        assert f.next() is None

    def test_next_skips_non_queued_urls(self):
        f = SharedFrontier()
        f.add("https://example.com/a", priority=2.0)
        f.add("https://example.com/b", priority=1.0)
        f.mark_in_flight("https://example.com/a")
        record = f.next()
        assert record is not None
        assert record.url == "https://example.com/b"


class TestSharedFrontierStateTransitions:
    def test_mark_in_flight(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_in_flight("https://example.com/a")
        assert f._records["https://example.com/a"].status == URLStatus.IN_FLIGHT

    def test_mark_extracted_with_records(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_extracted("https://example.com/a", records_count=5)
        rec = f._records["https://example.com/a"]
        assert rec.status == URLStatus.EXTRACTED
        assert rec.records_count == 5

    def test_mark_extracted_resets_consecutive_failures(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.add("https://example.com/b")
        f.mark_extracted("https://example.com/a", records_count=0)
        f.mark_extracted("https://example.com/b", records_count=3)
        assert f._consecutive_failures == 0

    def test_mark_sampled_status(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_sampled("https://example.com/a", records_count=1)
        assert f._records["https://example.com/a"].status == URLStatus.SAMPLED

    def test_mark_sampled_url_not_yet_in_frontier(self):
        f = SharedFrontier()
        f.mark_sampled("https://example.com/new", records_count=1)
        assert "https://example.com/new" in f._records
        assert f._records["https://example.com/new"].status == URLStatus.SAMPLED

    def test_mark_failed(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_failed("https://example.com/a", reason="403")
        rec = f._records["https://example.com/a"]
        assert rec.status == URLStatus.FAILED
        assert rec.failure_reason == "403"

    def test_next_skips_sampled(self):
        f = SharedFrontier()
        f.add("https://example.com/sampled", priority=2.0)
        f.add("https://example.com/queued", priority=1.0)
        f.mark_sampled("https://example.com/sampled")
        record = f.next()
        assert record is not None
        assert record.url == "https://example.com/queued"

    def test_next_skips_extracted(self):
        f = SharedFrontier()
        f.add("https://example.com/done", priority=2.0)
        f.add("https://example.com/todo", priority=1.0)
        f.mark_extracted("https://example.com/done", records_count=5)
        record = f.next()
        assert record is not None
        assert record.url == "https://example.com/todo"

    def test_next_skips_failed(self):
        f = SharedFrontier()
        f.add("https://example.com/failed", priority=2.0)
        f.add("https://example.com/ok", priority=1.0)
        f.mark_failed("https://example.com/failed")
        record = f.next()
        assert record is not None
        assert record.url == "https://example.com/ok"


class TestQualitySignals:
    def test_no_failures_no_reexplore(self):
        f = SharedFrontier()
        for i in range(5):
            f.add(f"https://example.com/{i}")
            f.mark_extracted(f"https://example.com/{i}", records_count=3)
        sigs = f.quality_signals()
        assert sigs.needs_reexplore() is False

    def test_3_consecutive_failures_triggers_reexplore(self):
        f = SharedFrontier()
        for i in range(3):
            f.add(f"https://example.com/{i}")
            f.mark_extracted(f"https://example.com/{i}", records_count=0)
        sigs = f.quality_signals()
        assert sigs.consecutive_failures == 3
        assert sigs.needs_reexplore() is True

    def test_high_empty_rate_triggers_reexplore(self):
        f = SharedFrontier()
        # 4 empty out of 5 = 80% empty rate
        for i in range(4):
            f.add(f"https://example.com/empty{i}")
            f.mark_extracted(f"https://example.com/empty{i}", records_count=0)
        f.add("https://example.com/good")
        f.mark_extracted("https://example.com/good", records_count=1)
        # reset consecutive_failures manually since last one was good
        sigs = f.quality_signals()
        assert sigs.total_extracted == 5
        assert sigs.empty_rate == pytest.approx(0.8)
        assert sigs.needs_reexplore() is True

    def test_consecutive_failures_resets_on_success(self):
        f = SharedFrontier()
        for i in range(2):
            f.add(f"https://example.com/empty{i}")
            f.mark_extracted(f"https://example.com/empty{i}", records_count=0)
        f.add("https://example.com/good")
        f.mark_extracted("https://example.com/good", records_count=5)
        sigs = f.quality_signals()
        assert sigs.consecutive_failures == 0


class TestSharedFrontierData:
    def test_ingest_data_deduplicates(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        data = [{"title": "foo", "js_code": "x" * 200}]
        f.mark_extracted("https://example.com/a", records_count=1, new_data=data)
        f.add("https://example.com/b")
        f.mark_extracted("https://example.com/b", records_count=1, new_data=data)
        assert len(f.all_data()) == 1  # deduplicated by fingerprint

    def test_all_data_returns_copy(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_extracted("https://example.com/a", records_count=1,
                         new_data=[{"title": "a"}])
        data = f.all_data()
        data.append({"title": "injected"})
        assert len(f.all_data()) == 1  # original not modified


class TestSeedFromIntel:
    def test_seed_from_intel_with_none(self):
        f = SharedFrontier()
        added = f.seed_from_intel(None, "https://example.com")
        assert added == 0
        assert "https://example.com" in f._records

    def test_seed_from_intel_priorities(self):
        class FakeScoredURL:
            def __init__(self, url):
                self.url = url

        class FakeIntel:
            direct_content = [FakeScoredURL("https://example.com/pen/abc")]
            entry_points = [FakeScoredURL("https://example.com/trending")]

        f = SharedFrontier()
        f.seed_from_intel(FakeIntel(), "https://example.com")
        assert f._records["https://example.com/pen/abc"].priority == 2.5
        assert f._records["https://example.com/trending"].priority == 1.5
        assert f._records["https://example.com"].priority == 1.0


class TestGetFailureSummary:
    def test_empty_when_no_failures(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_extracted("https://example.com/a", records_count=3)
        assert f.get_failure_summary() == ""

    def test_includes_failed_urls(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_failed("https://example.com/a", reason="403")
        summary = f.get_failure_summary()
        assert "https://example.com/a" in summary

    def test_includes_empty_extracted_urls(self):
        f = SharedFrontier()
        f.add("https://example.com/a")
        f.mark_extracted("https://example.com/a", records_count=0)
        summary = f.get_failure_summary()
        assert "https://example.com/a" in summary
