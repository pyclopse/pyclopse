"""pyclaw skills system — agentskills.io compatible skill discovery and injection."""

from .registry import (
    SkillInfo,
    discover_skills,
    find_skill,
    get_skill_dirs,
    format_for_prompt,
)

__all__ = [
    "SkillInfo",
    "discover_skills",
    "find_skill",
    "get_skill_dirs",
    "format_for_prompt",
]
