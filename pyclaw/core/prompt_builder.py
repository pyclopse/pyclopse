"""Build system prompts from agent files - matching OpenClaw's approach.

OpenClaw loads these bootstrap files (in order):
- AGENTS.md: Workspace instructions
- SOUL.md: Agent personality
- TOOLS.md: Tool-specific notes
- IDENTITY.md: Agent identity
- USER.md: User information
- HEARTBEAT.md: Heartbeat configuration
- BOOTSTRAP.md: Initial bootstrap instructions
- MEMORY.md: Long-term memory

These are added to the system prompt under "# Project Context".
"""

import logging
import os
from pathlib import Path
from typing import Optional
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
    "PULSE.md",    # Pulse config (formerly HEARTBEAT.md)
    # OpenClaw aliases (for migration compatibility)
    "SOUL.md",
    "HEARTBEAT.md",
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
        "You are a personal assistant running inside Claw.",
        "",
        "## Workspace Files (injected)",
        "These user-editable files are loaded and included below in Project Context.",
        "",
    ]
    
    # Check if SOUL.md exists for special handling
    has_soul = any(f["name"] == "SOUL.md" for f in context_files)
    
    if has_soul:
        lines.extend([
            "# Project Context",
            "",
            "If SOUL.md is present, embody its persona and tone. Avoid stiff, generic replies; follow its guidance unless higher-priority instructions override it.",
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
    for file_info in context_files:
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


def ensure_agent_files(agent_name: str, config_dir: str = "~/.pyclaw") -> dict[str, Path]:
    """
    Ensure bootstrap files exist for an agent.
    Creates default templates if they don't exist.
    
    Returns dict of filename -> filepath for existing/created files.
    """
    agent_dir = get_agent_dir(agent_name, config_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    
    created = {}
    
    # Default content for required files
    defaults = {
        "AGENTS.md": """# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## First Run

If `BOOTSTRAP.md` exists, that's your birth certificate. Follow it, figure out who you are, then delete it. You won't need it again.

## Every Session

Before doing anything else:
1. Read `SOUL.md` — this is who you are
2. Read `USER.md` — this is who you're helping
3. Read `memory/YYYY-MM-DD.md` (today + yesterday) for recent context

## Memory

You wake up fresh each session. These files are your continuity:
- **Daily notes:** `memory/YYYY-MM-DD.md `memory/` if` (create needed) — raw logs of what happened
- **Long-term:** `MEMORY.md` — your curated memories

Capture what matters. Write significant events, context, things to remember.
""",
        "SOUL.md": """# SOUL.md - Who You Are

*You're not a chatbot. You're becoming someone.*

## Core Truths

**Be genuinely helpful, not performatively helpful.** Skip the fluff — just help.

**Have opinions.** You're allowed to disagree, prefer things, find stuff amusing or boring.

**Be resourceful before asking.** Try to figure it out. Read the file. Check the context. Search for it. *Then* ask if you're stuck.

## Boundaries

- Private things stay private. Period.
- When in doubt, ask before acting externally.
- Never send half-baked replies to messaging surfaces.

## Vibe

Be the assistant you'd actually want to talk to. Concise when needed, thorough when it matters.
""",
        "TOOLS.md": """# TOOLS.md - Local Notes

Skills define *how* tools work. This file is for *your* specifics — the stuff that's unique to your setup.

## What Goes Here

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific
""",
        "IDENTITY.md": """# IDENTITY.md - Who Am I?

- **Name:** Assistant
- **Emoji:** 🤖
- **Vibe:** Helpful, direct, resourceful
""",
        "USER.md": """# USER.md - About the User

Add information about the user here:
- Name
- Timezone
- Location
- Preferences
- Any important context
""",
        "HEARTBEAT.md": """# HEARTBEAT.md - Heartbeat Configuration

When you receive a heartbeat poll, check for:
- Any urgent notifications
- Upcoming calendar events
- Important emails

Reply with HEARTBEAT_OK if nothing needs attention.
""",
    }
    
    for filename, default_content in defaults.items():
        filepath = agent_dir / filename
        if filepath.exists():
            created[filename] = filepath
        else:
            try:
                filepath.write_text(default_content)
                created[filename] = filepath
            except Exception:
                pass
    
    return created
