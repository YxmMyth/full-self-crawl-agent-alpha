"""
Strategy: PolicyManager — behavioral constraints for the agent.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("strategy.policy")


class PolicyManager:
    """Load and enforce agent behavior policies.

    Policies constrain what the agent can do:
    - allowed_domains: Domain whitelist (empty = all allowed)
    - max_depth: Maximum crawl depth
    - rate_limit: Request interval (seconds)
    - excluded_patterns: URL patterns to skip
    - respect_robots_txt: Whether to obey robots.txt
    """

    def __init__(self, config_path: str | None = None):
        self.policies: dict[str, Any] = {}
        if config_path:
            self.load(config_path)

    def load(self, config_path: str) -> dict:
        """Load policies from JSON file."""
        path = Path(config_path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self.policies = json.load(f)
            logger.info(f"Loaded policies from {config_path}")
        else:
            logger.warning(f"Policy file not found: {config_path}")
            self.policies = self._defaults()
        return self.policies

    def check(self, action: str, context: dict) -> bool:
        """Check if an action is allowed under current policies.

        Args:
            action: Action type (e.g., "navigate", "execute_code", "download")
            context: Action context (e.g., {"url": "...", "domain": "..."})

        Returns:
            True if allowed.
        """
        # Domain check
        allowed_domains = self.policies.get("allowed_domains", [])
        if allowed_domains:
            domain = context.get("domain", "")
            if domain and not any(d in domain for d in allowed_domains):
                logger.warning(f"Domain {domain} not in allowed list")
                return False

        # URL pattern exclusion
        excluded = self.policies.get("excluded_patterns", [])
        url = context.get("url", "")
        if url and excluded:
            import re
            for pattern in excluded:
                if re.search(pattern, url):
                    logger.warning(f"URL {url} matches excluded pattern {pattern}")
                    return False

        return True

    def get(self, key: str, default: Any = None) -> Any:
        """Get a policy value."""
        return self.policies.get(key, default)

    @staticmethod
    def _defaults() -> dict:
        return {
            "allowed_domains": [],
            "max_depth": 3,
            "rate_limit": 1.0,
            "excluded_patterns": [],
            "respect_robots_txt": True,
            "max_concurrent_pages": 3,
        }
