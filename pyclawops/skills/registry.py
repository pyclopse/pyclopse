"""Skill discovery and formatting for pyclawops.


Skills follow the agentskills.io specification:
  - Each skill is a directory containing a SKILL.md file
  - SKILL.md has YAML frontmatter with at minimum: name, description
  - Supporting files (scripts, data) live alongside SKILL.md

Directory search order:
  1. Global:     ~/.pyclawops/skills/
  2. Per-agent:  ~/.pyclawops/agents/{agent_name}/skills/
  3. Extra dirs from config: gateway.skills_dirs
"""

from __future__ import annotations
from pyclawops.reflect import reflect_system

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Token substituted with the absolute skill directory path at read-time
_SKILL_DIR_TOKEN = "{skill_dir}"


@dataclass
@reflect_system("skills")
class SkillInfo:
    """A discovered skill with its parsed metadata and on-disk location.

    Instances are produced by :func:`_parse_skill_dir` and collected by
    :func:`discover_skills`.  Callers should use :meth:`read_content` or
    :meth:`read_body` to obtain the actual skill instructions with the
    ``{skill_dir}`` placeholder substituted.

    Attributes:
        name (str): Canonical skill name from the YAML frontmatter.
        description (str): Short description from the YAML frontmatter.
        path (Path): Absolute path to the skill directory.
        skill_md (Path): Absolute path to the ``SKILL.md`` file.
        body (str): Markdown body of ``SKILL.md`` after the frontmatter block.
        version (str): Optional version string from frontmatter.
        license (str): Optional license string from frontmatter.
        compatibility (str): Optional compatibility string from frontmatter.
        allowed_tools (list[str]): MCP tool names the skill is permitted to use
            (parsed from the space-delimited ``allowed-tools`` frontmatter field).
        metadata (dict): Freeform metadata dict from the frontmatter
            ``metadata`` key.
    """

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
        """Return the full SKILL.md content with ``{skill_dir}`` substituted.

        Reads the raw file from disk and replaces every occurrence of the
        ``{skill_dir}`` token with the absolute path of the skill directory
        so that relative paths inside the skill always resolve correctly.

        Returns:
            str: Full SKILL.md text with the ``{skill_dir}`` token replaced.
        """
        raw = self.skill_md.read_text()
        return raw.replace(_SKILL_DIR_TOKEN, str(self.path))

    def read_body(self) -> str:
        """Return the body (post-frontmatter) with ``{skill_dir}`` substituted.

        Applies the same ``{skill_dir}`` substitution as :meth:`read_content`
        but only to the pre-parsed body string stored in :attr:`body` rather
        than re-reading the file.

        Returns:
            str: Markdown body text with the ``{skill_dir}`` token replaced.
        """
        return self.body.replace(_SKILL_DIR_TOKEN, str(self.path))


def get_skill_dirs(
    agent_name: Optional[str] = None,
    config_dir: str = "~/.pyclawops",
    extra_dirs: Optional[list[str]] = None,
) -> list[Path]:
    """Return the ordered list of skill search directories that exist on disk.

    Directories are returned in precedence order (lowest to highest):
    global skills directory, per-agent skills directory, then any extra
    directories supplied via config.

    Args:
        agent_name (Optional[str]): Agent name used to derive the per-agent
            skills path (``{config_dir}/agents/{agent_name}/skills``).
            When None, only the global directory is included.
        config_dir (str): Base pyclawops config directory. Defaults to
            ``"~/.pyclawops"``.
        extra_dirs (Optional[list[str]]): Additional directories to append
            at the end of the search path (highest precedence).

    Returns:
        list[Path]: Resolved absolute paths to directories that exist.
    """
    base = Path(config_dir).expanduser()
    candidates: list[Path] = [base / "skills"]
    if agent_name:
        candidates.append(base / "agents" / agent_name / "skills")
    if extra_dirs:
        for d in extra_dirs:
            candidates.append(Path(d).expanduser())
    return [p for p in candidates if p.exists() and p.is_dir()]


def _parse_skill_dir(skill_dir: Path) -> Optional[SkillInfo]:
    """Parse a single skill directory and return a :class:`SkillInfo`.

    Expects the directory to contain a ``SKILL.md`` file with YAML
    frontmatter that includes at minimum ``name`` and ``description`` fields.
    Returns None and logs a warning on any error.

    Args:
        skill_dir (Path): Path to the skill directory to parse.

    Returns:
        Optional[SkillInfo]: Populated :class:`SkillInfo` on success, or
            None if the directory is not a valid skill (missing ``SKILL.md``,
            missing required frontmatter fields, or any parse error).
    """
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
    config_dir: str = "~/.pyclawops",
    extra_dirs: Optional[list[str]] = None,
) -> list[SkillInfo]:
    """Discover all skills from global and agent-specific skill directories.

    Searches all directories returned by :func:`get_skill_dirs` and
    deduplicates by lowercase skill name.  Later directories in the search
    path take precedence — agent-specific skills override globals with the
    same name.

    Args:
        agent_name (Optional[str]): Agent name to include per-agent skills.
            When None, only global skills are discovered.
        config_dir (str): Base pyclawops config directory. Defaults to
            ``"~/.pyclawops"``.
        extra_dirs (Optional[list[str]]): Additional directories appended
            at the highest precedence level.

    Returns:
        list[SkillInfo]: All discovered skills in their final deduplicated
            form (last writer wins on name collision).
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
    config_dir: str = "~/.pyclawops",
    extra_dirs: Optional[list[str]] = None,
) -> Optional[SkillInfo]:
    """Find a skill by name (case-insensitive).

    Runs a full :func:`discover_skills` pass and returns the first match
    whose lowercased name equals ``name.lower().strip()``.

    Args:
        name (str): Skill name to search for (case-insensitive).
        agent_name (Optional[str]): Agent name for per-agent skill lookup.
        config_dir (str): Base pyclawops config directory. Defaults to
            ``"~/.pyclawops"``.
        extra_dirs (Optional[list[str]]): Additional search directories.

    Returns:
        Optional[SkillInfo]: The matching :class:`SkillInfo`, or None if no
            skill with that name exists.
    """
    key = name.lower().strip()
    for skill in discover_skills(agent_name, config_dir, extra_dirs):
        if skill.name.lower() == key:
            return skill
    return None


def format_for_prompt(
    skills: list[SkillInfo],
    read_tool_name: str = "skill_read",
) -> str:
    """Format a skill list as the lean ``<available_skills>`` XML block.

    The preamble instructs the agent to call *read_tool_name* to fetch the
    full ``SKILL.md`` before acting on any skill.  Returns an empty string
    when *skills* is empty so callers can safely skip injection.

    Args:
        skills (list[SkillInfo]): Skills to include in the block.
        read_tool_name (str): MCP tool name the agent should call to read a
            skill's full instructions. Defaults to ``"skill_read"``.

    Returns:
        str: Preamble plus ``<available_skills>`` XML block ready for
            insertion into a system prompt, or ``""`` if *skills* is empty.
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
