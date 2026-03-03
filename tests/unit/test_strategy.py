"""
Unit tests for strategy layer: CrawlSpec, SpecInferrer, PolicyManager, CompletionGate.
"""

import pytest
from src.strategy.spec import CrawlSpec, SpecLoader, _safe_parse_json
from src.strategy.policy import PolicyManager
from src.strategy.gate import CompletionGate, GateDecision


class TestCrawlSpec:
    def test_create_minimal(self):
        spec = CrawlSpec(url="https://example.com", requirement="Get products")
        assert spec.url == "https://example.com"
        assert spec.min_items == 10
        assert spec.quality_threshold == 0.7
        assert spec.mode == "full_site"
        assert spec.target_fields is None

    def test_roundtrip_dict(self):
        spec = CrawlSpec(
            url="https://x.com", requirement="Find articles",
            understanding="Articles about tech",
            target_fields=[{"name": "title"}, {"name": "date"}],
            min_items=5,
        )
        d = spec.to_dict()
        spec2 = CrawlSpec.from_dict(d)
        assert spec2.requirement == spec.requirement
        assert spec2.target_fields == spec.target_fields
        assert spec2.min_items == 5


class TestSpecLoader:
    def test_from_dict(self):
        spec = SpecLoader.from_dict("https://x.com", {
            "requirement": "Find articles",
            "min_items": 5,
        })
        assert spec.requirement == "Find articles"
        assert spec.min_items == 5

    def test_from_dict_with_goal_alias(self):
        spec = SpecLoader.from_dict("https://x.com", {"goal": "Find stuff"})
        assert spec.requirement == "Find stuff"


class TestSafeParseJson:
    def test_valid_json(self):
        assert _safe_parse_json('{"a": 1}') == {"a": 1}

    def test_markdown_code_block(self):
        result = _safe_parse_json('```json\n{"b": 2}\n```')
        assert result == {"b": 2}

    def test_garbage(self):
        assert _safe_parse_json("not json at all") == {}

    def test_embedded_json(self):
        result = _safe_parse_json('Here is the result: {"key": "value"} done')
        assert result == {"key": "value"}


class TestPolicyManager:
    def test_default_allows_everything(self):
        pm = PolicyManager()
        assert pm.check("navigate", {"domain": "any.com"})

    def test_domain_restriction(self):
        pm = PolicyManager()
        pm.policies = {"allowed_domains": ["example.com"]}
        assert pm.check("navigate", {"domain": "example.com"})
        assert not pm.check("navigate", {"domain": "evil.com"})

    def test_url_exclusion(self):
        pm = PolicyManager()
        pm.policies = {"excluded_patterns": [r"\.pdf$", r"logout"]}
        assert not pm.check("download", {"url": "https://example.com/file.pdf"})
        assert not pm.check("navigate", {"url": "https://example.com/logout"})
        assert pm.check("navigate", {"url": "https://example.com/page"})


class TestCompletionGate:
    def test_met_when_enough_data(self):
        gate = CompletionGate()
        spec = CrawlSpec(url="x", requirement="y",
                         target_fields=[{"name": "a"}, {"name": "b"}],
                         min_items=2)
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        dec = gate.check(data, spec)
        assert dec.met is True
        assert dec.current_quality == 1.0

    def test_not_met_insufficient_items(self):
        gate = CompletionGate()
        spec = CrawlSpec(url="x", requirement="y", min_items=10)
        data = [{"a": 1}]
        dec = gate.check(data, spec)
        assert dec.met is False
        assert "items" in dec.reason

    def test_not_met_low_quality(self):
        gate = CompletionGate()
        spec = CrawlSpec(url="x", requirement="y",
                         target_fields=[{"name": "a"}, {"name": "b"}],
                         min_items=2, quality_threshold=0.8)
        data = [{"a": 1}, {"a": 2}]  # b always missing → 50% quality
        dec = gate.check(data, spec)
        assert dec.met is False

    def test_empty_data(self):
        gate = CompletionGate()
        spec = CrawlSpec(url="x", requirement="y")
        dec = gate.check([], spec)
        assert dec.met is False
        assert dec.current_quality == 0.0
