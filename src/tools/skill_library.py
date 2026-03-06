"""
Tools: SkillLibrary — persistent storage of verified extraction strategies.

Inspired by Voyager's Skill Library concept: successful extraction strategies
are stored and indexed by URL pattern, then injected into the agent's context
when a matching URL is encountered. The agent still calls evaluate_js/execute_code
with the skill code — it reasons, it's not a hardcoded black box.
"""

import json
import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

logger = logging.getLogger("tools.skill_library")

_DEFAULT_SKILLS_PATH = Path(__file__).parent.parent.parent / "skills" / "skills.json"


class SkillLibrary:
    """Load, match, and persist extraction skills.

    Skills are keyed by URL glob pattern (e.g. 'codepen.io/*/pen/*').
    When a URL matches, the skill's code is injected into the LLM context
    as a "previously verified strategy" — the agent can use it as-is or adapt it.
    """

    def __init__(self, skills_path: Path | str | None = None):
        self.path = Path(skills_path) if skills_path else _DEFAULT_SKILLS_PATH
        self._skills: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._skills = json.load(f)
                logger.debug(f"Loaded {len(self._skills)} skill(s) from {self.path}")
            except Exception as e:
                logger.warning(f"Could not load skills from {self.path}: {e}")
                self._skills = []

    def get_relevant_skills(self, url: str, role: str | None = None) -> list[dict]:
        """Return skills whose url_pattern matches the given URL.

        Pattern matching uses fnmatch glob syntax:
          'codepen.io/*/pen/*'  matches  'codepen.io/user123/pen/abcXYZ'

        If role is provided, only return skills with matching role
        (skills without a 'role' field default to 'extraction' for backward compat).
        """
        matched = []
        for skill in self._skills:
            pattern = skill.get("url_pattern", "")
            if not pattern or not fnmatch(url, f"*{pattern}*"):
                continue
            skill_role = skill.get("role", "extraction")
            if role is not None and skill_role != role:
                continue
            matched.append(skill)
        return matched

    def save_skill(self, skill: dict) -> None:
        """Add or update a skill, then persist to disk.

        If a skill with the same id already exists, increments verified_count.
        Otherwise appends as a new skill.
        """
        for existing in self._skills:
            if existing.get("id") == skill.get("id"):
                existing["verified_count"] = existing.get("verified_count", 0) + 1
                self._persist()
                logger.info(f"Skill '{skill['id']}' verified_count now {existing['verified_count']}")
                return
        self._skills.append(skill)
        self._persist()
        logger.info(f"Saved new skill '{skill.get('id')}'")

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._skills, f, indent=2, ensure_ascii=False)

    def format_for_prompt(self, skills: list[dict]) -> str:
        """Format matched skills as a prompt-injectable string."""
        if not skills:
            return ""
        lines = ["⚡ Verified strategies for this URL — use these:"]
        for s in skills:
            lines.append(f"\n[{s['name']}] (verified {s.get('verified_count', 1)}x)")
            if s.get("code"):
                # Extraction skill: show the JS code to call
                lines.append(f"  Call: {s['tool']}(script=<code below>)")
                lines.append(f"  Code: {s['code']}")
            elif s.get("steps"):
                # Multi-step skill
                for step in s["steps"]:
                    lines.append(f"  Step: {step}")
            elif s.get("tool"):
                # Exploration/navigation skill: just recommend the tool
                lines.append(f"  Use: {s['tool']}() — not get_html()")
            lines.append(f"  Why: {s['description']}")
        return "\n".join(lines)
