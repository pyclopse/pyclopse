"""Build system prompts from agent files - matching OpenClaw's approach.

OpenClaw loads these bootstrap files (in order):
- AGENTS.md: Workspace instructions
- SOUL.md: Agent personality
- TOOLS.md: Tool-specific notes
- IDENTITY.md: Agent identity
- USER.md: User information
- BOOTSTRAP.md: Initial bootstrap instructions
- MEMORY.md: Long-term memory

These are added to the system prompt under "# Project Context".
"""

import logging
import os
from pathlib import Path
from typing import Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# Files to read for system prompt - pyclaw names with OpenClaw aliases
BOOTSTRAP_FILES = [
    # pyclaw names (preferred)
    "AGENTS.md",     # Workspace instructions
    "PERSONALITY.md", # Agent personality (formerly SOUL.md)
    "IDENTITY.md",   # Agent identity
    "RULES.md",     # Operational rules (pyclaw addition)
    "USER.md",      # User information
    # OpenClaw aliases (for migration compatibility)
    "SOUL.md",
    "BOOTSTRAP.md", # Initial bootstrap (often deleted after first run)
    "MEMORY.md",    # Long-term memory
    "memory.md",    # Alternate memory filename
]

# Backward compatibility alias
AGENT_FILES = BOOTSTRAP_FILES

# Minimal files for subagent/cron sessions (matching OpenClaw's MINIMAL_BOOTSTRAP_ALLOWLIST)
MINIMAL_BOOTSTRAP_FILES = [
    "AGENTS.md",
    "TOOLS.md", 
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
]


def get_agent_dir(agent_name: str, config_dir: str = "~/.pyclaw") -> Path:
    """Get the agent's directory."""
    return Path(config_dir).expanduser() / "agents" / agent_name


def get_workspace_dir(config_dir: str = "~/.pyclaw") -> Path:
    """Get the workspace directory."""
    return Path(config_dir).expanduser() / "workspace"


def read_bootstrap_file(filepath: Path) -> Optional[str]:
    """Read a bootstrap file if it exists."""
    if filepath.exists():
        try:
            content = filepath.read_text().strip()
            return content if content else None
        except Exception:
            pass
    return None


def build_system_prompt(
    agent_name: str,
    config_dir: str = "~/.pyclaw",
    default_prompt: Optional[str] = None,
    include_memory: bool = True,
    is_subagent: bool = False,
    extra_skill_dirs: Optional[list] = None,
) -> str:
    """
    Build system prompt from agent files - matching OpenClaw's approach.
    
    Args:
        agent_name: Name of the agent
        config_dir: Base config directory
        default_prompt: Fallback prompt if no files exist
        include_memory: Whether to include MEMORY.md
        is_subagent: If True, use minimal bootstrap (for subagents/cron)
    
    Returns:
        Complete system prompt string
    """
    agent_dir = get_agent_dir(agent_name, config_dir)
    
    if not agent_dir.exists():
        if default_prompt:
            return default_prompt
        return "You are a helpful AI assistant."
    
    # Determine which files to load
    files_to_load = MINIMAL_BOOTSTRAP_FILES if is_subagent else BOOTSTRAP_FILES
    
    context_files = []
    
    for filename in files_to_load:
        # Skip memory files if not including memory
        if not include_memory and filename in ("MEMORY.md", "memory.md"):
            continue
            
        filepath = agent_dir / filename
        content = read_bootstrap_file(filepath)
        
        if content:
            # Store path relative to workspace for display
            context_files.append({
                "path": str(filepath),
                "name": filename,
                "content": content,
            })
    
    # If no files found, use default
    if not context_files:
        if default_prompt:
            return default_prompt
        return "You are a helpful AI assistant."
    
    # Build system prompt matching OpenClaw structure
    lines = [
        "You are a personal assistant running inside pyclaw.",
        "",
        "## Workspace Files (injected)",
        "These user-editable files are loaded and included below in Project Context.",
        "",
    ]

    # Check if SOUL.md or PERSONALITY.md exists for special handling
    has_soul = any(f["name"] in ("SOUL.md", "PERSONALITY.md") for f in context_files)

    if has_soul:
        lines.extend([
            "# Project Context",
            "",
            "If PERSONALITY.md (or SOUL.md) is present, embody its persona and tone. Avoid stiff, generic replies; follow its guidance unless higher-priority instructions override it.",
            "",
        ])
    else:
        lines.extend([
            "# Project Context",
            "",
            "The following context files have been loaded:",
            "",
        ])

    # Add each file as a section (matching OpenClaw's format)
    # RULES.md gets special framing to ensure the agent treats it as authoritative.
    for file_info in context_files:
        if file_info["name"] == "RULES.md":
            lines.extend([
                f"## {file_info['path']}",
                "",
                "**IMPORTANT: The following rules were set by the user. They are mandatory and must be followed at all times.**",
                "",
                file_info["content"],
                "",
            ])
        else:
            lines.extend([
                f"## {file_info['path']}",
                "",
                file_info["content"],
                "",
            ])
    
    # Append lean <available_skills> block (skipped for subagents to keep prompts small)
    if not is_subagent:
        try:
            from pyclaw.skills.registry import discover_skills, format_for_prompt
            skills = discover_skills(
                agent_name=agent_name,
                config_dir=config_dir,
                extra_dirs=extra_skill_dirs,
            )
            if skills:
                lines.append("")
                lines.append(format_for_prompt(skills))
        except Exception as e:
            logger.debug(f"Skill injection skipped: {e}")

    return "\n".join(lines)


def build_job_prompt(
    agent_name: str,
    config_dir: str = "~/.pyclaw",
    agent_run: Any = None,
    extra_dirs: Optional[list] = None,
) -> str:
    """Build a system prompt for a job run based on AgentRun include_* flags.

    Reads bootstrap files according to the resolved flags on *agent_run*, then
    appends ``agent_run.instruction`` if set.  Works with any prompt_preset
    (full / minimal / task) plus any per-field overrides.
    """
    agent_dir = get_agent_dir(agent_name, config_dir)

    # Map each flag to the ordered list of filenames to try (first match wins)
    FLAG_FILES = [
        ("include_personality", ["PERSONALITY.md", "SOUL.md"]),
        ("include_identity",    ["IDENTITY.md"]),
        ("include_rules",       ["RULES.md"]),
        ("include_memory",      ["MEMORY.md", "memory.md"]),
        ("include_user",        ["USER.md"]),
        ("include_agents",      ["AGENTS.md"]),
        ("include_tools",       ["TOOLS.md"]),
    ]

    context_files = []
    if agent_run is not None and agent_dir.exists():
        for flag, filenames in FLAG_FILES:
            if not getattr(agent_run, flag, False):
                continue
            for filename in filenames:
                content = read_bootstrap_file(agent_dir / filename)
                if content:
                    context_files.append({
                        "path": str(agent_dir / filename),
                        "name": filename,
                        "content": content,
                    })
                    break  # first match per flag

    lines: List[str] = []

    if context_files:
        lines += [
            "You are a personal assistant running inside pyclaw.",
            "",
            "# Project Context",
            "",
        ]
        for file_info in context_files:
            if file_info["name"] == "RULES.md":
                lines += [
                    f"## {file_info['path']}",
                    "",
                    "**IMPORTANT: The following rules were set by the user."
                    " They are mandatory and must be followed at all times.**",
                    "",
                    file_info["content"],
                    "",
                ]
            else:
                lines += [
                    f"## {file_info['path']}",
                    "",
                    file_info["content"],
                    "",
                ]

    # Skills block
    if agent_run is not None and getattr(agent_run, "include_skills", False):
        try:
            from pyclaw.skills.registry import discover_skills, format_for_prompt
            skills = discover_skills(agent_name=agent_name, config_dir=config_dir, extra_dirs=extra_dirs)
            skill_filter = getattr(agent_run, "skills", None)
            if skill_filter:
                skill_filter_lower = {s.lower() for s in skill_filter}
                skills = [s for s in skills if s.name.lower() in skill_filter_lower]
            if skills:
                lines.append(format_for_prompt(skills))
                lines.append("")
        except Exception as e:
            logger.debug(f"Skill injection skipped in job prompt: {e}")

    # Append instruction (always, even if everything else is empty)
    instruction = getattr(agent_run, "instruction", None) if agent_run is not None else None
    if instruction:
        if lines:
            lines += ["## Job Instruction", ""]
        lines.append(instruction)

    # Inject additional files into the system prompt (after instruction)
    include_files = getattr(agent_run, "include_files", None) if agent_run is not None else None
    if include_files:
        for file_path in include_files:
            path = Path(os.path.expanduser(file_path))
            content = read_bootstrap_file(path)
            if content:
                lines += [
                    "",
                    f"## {path}",
                    "",
                    content,
                    "",
                ]
            else:
                logger.debug(f"include_files: skipping missing or empty file: {path}")

    return "\n".join(lines) if lines else "You are a helpful AI assistant."


def build_minimal_system_prompt(
    agent_name: str,
    config_dir: str = "~/.pyclaw",
) -> str:
    """Build minimal system prompt for subagents (matching OpenClaw's minimal mode)."""
    return build_system_prompt(
        agent_name=agent_name,
        config_dir=config_dir,
        include_memory=False,
        is_subagent=True,
    )


def get_agent_file_path(agent_name: str, filename: str, config_dir: str = "~/.pyclaw") -> Optional[Path]:
    """Get path to a specific agent file."""
    filepath = get_agent_dir(agent_name, config_dir) / filename
    if filepath.exists():
        return filepath
    return None


def list_agent_files(agent_name: str, config_dir: str = "~/.pyclaw") -> list[str]:
    """List all files in agent directory."""
    agent_dir = get_agent_dir(agent_name, config_dir)
    if agent_dir.exists():
        return [f.name for f in agent_dir.iterdir() if f.is_file()]
    return []


def _get_templates_dir() -> Path:
    """Return the path to the bundled templates directory."""
    return Path(__file__).parent / "templates"


def ensure_agent_files(agent_name: str, config_dir: str = "~/.pyclaw") -> dict[str, Path]:
    """
    Ensure bootstrap files exist for an agent.
    Creates default templates if they don't exist.

    Returns dict of filename -> filepath for existing/created files.
    """
    agent_dir = get_agent_dir(agent_name, config_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = _get_templates_dir()
    template_files = [
        "AGENTS.md",
        "SOUL.md",
        "TOOLS.md",
        "IDENTITY.md",
        "USER.md",
        "BOOTSTRAP.md",
    ]

    created = {}

    for filename in template_files:
        filepath = agent_dir / filename
        if filepath.exists():
            created[filename] = filepath
        else:
            template_path = templates_dir / filename
            try:
                if template_path.exists():
                    content = template_path.read_text()
                else:
                    content = f"# {filename}\n\nEdit this file to customize your agent.\n"
                filepath.write_text(content)
                created[filename] = filepath
            except Exception:
                pass

    return created
