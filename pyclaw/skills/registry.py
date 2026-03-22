"""Skill discovery and formatting for pyclaw.

Skills follow the agentskills.io specification:
  - Each skill is a directory containing a SKILL.md file
  - SKILL.md has YAML frontmatter with at minimum: name, description
  - Supporting files (scripts, data) live alongside SKILL.md

Directory search order:
  1. Global:     ~/.pyclaw/skills/
  2. Per-agent:  ~/.pyclaw/agents/{agent_name}/skills/
  3. Extra dirs from config: gateway.skills_dirs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Token substituted with the absolute skill directory path at read-time
_SKILL_DIR_TOKEN = "{skill_dir}"


@dataclass
class SkillInfo:
    """A discovered skill with its metadata and location."""

    name: str
    description: str
    path: Path          # Absolute path to the skill directory
    skill_md: Path      # Absolute path to SKILL.md
    body: str           # SKILL.md body (after frontmatter)
    version: str = ""
    license: str = ""
    compatibility: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def read_content(self) -> str:
        """Return the full SKILL.md content with {skill_dir} substituted."""
        raw = self.skill_md.read_text()
        return raw.replace(_SKILL_DIR_TOKEN, str(self.path))

    def read_body(self) -> str:
        """Return the body (post-frontmatter) with {skill_dir} substituted."""
        return self.body.replace(_SKILL_DIR_TOKEN, str(self.path))


def get_skill_dirs(
    agent_name: Optional[str] = None,
    config_dir: str = "~/.pyclaw",
    extra_dirs: Optional[list[str]] = None,
) -> list[Path]:
    """Return the ordered list of skill search directories that exist."""
    base = Path(config_dir).expanduser()
    candidates: list[Path] = [base / "skills"]
    if agent_name:
        candidates.append(base / "agents" / agent_name / "skills")
    if extra_dirs:
        for d in extra_dirs:
            candidates.append(Path(d).expanduser())
    return [p for p in candidates if p.exists() and p.is_dir()]


def _parse_skill_dir(skill_dir: Path) -> Optional[SkillInfo]:
    """Parse a single skill directory.  Returns None on any error."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        import frontmatter  # type: ignore
        post = frontmatter.loads(skill_md.read_text())
        meta = post.metadata or {}
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        if not name or not description:
            logger.warning(f"Skill missing name/description: {skill_md}")
            return None
        body = (post.content or "").strip()
        # Parse optional allowed-tools (space-delimited string per spec)
        raw_tools = meta.get("allowed-tools", "")
        allowed_tools = raw_tools.split() if isinstance(raw_tools, str) and raw_tools else []
        raw_meta = meta.get("metadata", {})
        metadata = raw_meta if isinstance(raw_meta, dict) else {}
        return SkillInfo(
            name=name,
            description=description,
            path=skill_dir.resolve(),
            skill_md=skill_md.resolve(),
            body=body,
            version=str(meta.get("version", "")),
            license=str(meta.get("license", "")),
            compatibility=str(meta.get("compatibility", "")),
            allowed_tools=allowed_tools,
            metadata=metadata,
        )
    except Exception as e:
        logger.warning(f"Failed to parse skill at {skill_dir}: {e}")
        return None


def discover_skills(
    agent_name: Optional[str] = None,
    config_dir: str = "~/.pyclaw",
    extra_dirs: Optional[list[str]] = None,
) -> list[SkillInfo]:
    """Discover all skills from global and agent-specific skill directories.

    Later directories take precedence — agent-specific skills override globals
    with the same name.
    """
    dirs = get_skill_dirs(agent_name, config_dir, extra_dirs)
    seen: dict[str, SkillInfo] = {}
    for skill_dir in dirs:
        for entry in sorted(skill_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill = _parse_skill_dir(entry)
            if skill:
                # Lower-cased name deduplication; last one wins
                seen[skill.name.lower()] = skill
    skills = list(seen.values())
    logger.debug(f"Discovered {len(skills)} skills for agent={agent_name!r}")
    return skills


def find_skill(
    name: str,
    agent_name: Optional[str] = None,
    config_dir: str = "~/.pyclaw",
    extra_dirs: Optional[list[str]] = None,
) -> Optional[SkillInfo]:
    """Find a skill by name (case-insensitive).  Returns None if not found."""
    key = name.lower().strip()
    for skill in discover_skills(agent_name, config_dir, extra_dirs):
        if skill.name.lower() == key:
            return skill
    return None


def format_for_prompt(
    skills: list[SkillInfo],
    read_tool_name: str = "skill_read",
) -> str:
    """Format skill list as the lean <available_skills> XML block.

    The preamble tells the agent to call *read_tool_name* to fetch the full
    SKILL.md before acting on a skill.
    """
    if not skills:
        return ""

    parts = []
    for s in skills:
        lines = ["<skill>", f"  <name>{s.name}</name>"]
        if s.description:
            lines.append(f"  <description>{s.description}</description>")
        if s.allowed_tools:
            lines.append(f"  <allowed_tools>{' '.join(s.allowed_tools)}</allowed_tools>")
        lines.append(f"  <location>{s.skill_md}</location>")
        lines.append("</skill>")
        parts.append("\n".join(lines))

    xml = "<available_skills>\n" + "\n".join(parts) + "\n</available_skills>"
    preamble = (
        "Skills provide specialised capabilities and domain knowledge. "
        "Use a skill if it seems relevant to the user's task or would increase your effectiveness.\n"
        f"To use a skill, call the '{read_tool_name}' MCP tool with the skill name to read its "
        "full instructions before acting.\n"
        "Only use skills listed in <available_skills> below.\n\n"
    )
    return preamble + xml
