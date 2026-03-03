"""
Strategy: CrawlSpec, SpecInferrer — the agent's "highest intent".

CrawlSpec is natural-language-first. The only required field is `requirement`.
Structured fields are soft constraints that may or may not match the website's
actual data format.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("strategy.spec")


@dataclass
class CrawlSpec:
    """Crawl specification — the agent's highest intent.

    Core is natural language. Structured fields are optional soft constraints.
    User's need and website's actual data may differ completely.
    """
    url: str
    requirement: str  # Only required field (natural language)
    understanding: str = ""
    success_criteria: str = ""
    exploration_hints: str = ""
    target_fields: list[dict] | None = None
    min_items: int = 10
    quality_threshold: float = 0.7
    mode: str = "full_site"  # full_site | single_page (testing only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "requirement": self.requirement,
            "understanding": self.understanding,
            "success_criteria": self.success_criteria,
            "exploration_hints": self.exploration_hints,
            "target_fields": self.target_fields,
            "min_items": self.min_items,
            "quality_threshold": self.quality_threshold,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CrawlSpec":
        return cls(
            url=d.get("url", ""),
            requirement=d.get("requirement", ""),
            understanding=d.get("understanding", ""),
            success_criteria=d.get("success_criteria", ""),
            exploration_hints=d.get("exploration_hints", ""),
            target_fields=d.get("target_fields"),
            min_items=d.get("min_items", 10),
            quality_threshold=d.get("quality_threshold", 0.7),
            mode=d.get("mode", "full_site"),
        )


class SpecInferrer:
    """Infer CrawlSpec from natural language requirement + URL.

    Uses LLM to understand user intent and generate structured spec.
    """

    INFER_PROMPT = """You are a web crawling specification expert. Given a URL and a user requirement,
produce a structured crawl specification.

URL: {url}
User requirement: {requirement}
{page_context}

Respond in JSON:
{{
    "understanding": "Your understanding of what the user wants (1-2 sentences)",
    "success_criteria": "What constitutes success (1-2 sentences)",
    "exploration_hints": "Hints for exploring the site to find data (1-2 sentences)",
    "target_fields": [
        {{"name": "field_name", "description": "what this field contains"}}
    ] or null if this is a file download / non-structured task,
    "min_items": <estimate based on the requirement — consider how many items per page and how many pages the user wants>,
    "quality_threshold": 0.7
}}"""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def infer(self, url: str, requirement: str,
                    page_html: str | None = None) -> CrawlSpec:
        """Infer CrawlSpec from user requirement."""
        page_context = ""
        if page_html:
            # Send first 2000 chars of HTML for context
            snippet = page_html[:2000]
            page_context = f"\nPage HTML snippet (first 2000 chars):\n```\n{snippet}\n```"

        prompt = self.INFER_PROMPT.format(
            url=url, requirement=requirement, page_context=page_context
        )

        try:
            response = await self.llm.generate(
                prompt=prompt,
                system_prompt="You are a precise JSON generator. Output only valid JSON.",
                max_tokens=1024,
                temperature=0.3,
            )
            data = _safe_parse_json(response)
        except Exception as e:
            logger.warning(f"LLM spec inference failed: {e}")
            data = {}

        return CrawlSpec(
            url=url,
            requirement=requirement,
            understanding=data.get("understanding", ""),
            success_criteria=data.get("success_criteria", ""),
            exploration_hints=data.get("exploration_hints", ""),
            target_fields=data.get("target_fields"),
            min_items=data.get("min_items", 10),
            quality_threshold=data.get("quality_threshold", 0.7),
        )


class SpecLoader:
    """Load CrawlSpec from dict or file."""

    @staticmethod
    def from_dict(url: str, spec_dict: dict) -> CrawlSpec:
        """Build CrawlSpec from user-provided dict."""
        return CrawlSpec(
            url=url,
            requirement=spec_dict.get("requirement", spec_dict.get("goal", "")),
            understanding=spec_dict.get("understanding", ""),
            success_criteria=spec_dict.get("success_criteria", ""),
            exploration_hints=spec_dict.get("exploration_hints", ""),
            target_fields=spec_dict.get("target_fields", spec_dict.get("targets")),
            min_items=spec_dict.get("min_items", 10),
            quality_threshold=spec_dict.get("quality_threshold", 0.7),
            mode=spec_dict.get("mode", "full_site"),
        )


def _safe_parse_json(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}
