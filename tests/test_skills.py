"""Tests for the pyclopse skill system.

Covers:
  - SkillInfo / parse from SKILL.md
  - discover_skills / find_skill
  - format_for_prompt
  - prompt_builder skill injection
  - MCP tools: skills_list, skill_read
  - /skills and /skill slash commands
"""
import os
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pyclopse.skills.registry import (
    SkillInfo,
    discover_skills,
    find_skill,
    format_for_prompt,
    get_skill_dirs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def skill_tree(tmp_path):
    """Create a temp skills directory tree with two skills."""
    skills_root = tmp_path / "skills"

    # Skill 1 — global, informational
    s1 = skills_root / "hello-world"
    s1.mkdir(parents=True)
    (s1 / "SKILL.md").write_text(
        "---\n"
        "name: hello-world\n"
        "description: Prints hello world\n"
        "version: 1.2.3\n"
        "---\n"
        "# Hello World Skill\n\n"
        "Run: `bash {skill_dir}/hello.sh`\n"
    )
    (s1 / "hello.sh").write_text("#!/bin/sh\necho 'hello world'\n")

    # Skill 2 — with allowed-tools frontmatter
    s2 = skills_root / "file-counter"
    s2.mkdir(parents=True)
    (s2 / "SKILL.md").write_text(
        "---\n"
        "name: file-counter\n"
        "description: Count files in a directory\n"
        "allowed-tools: bash\n"
        "---\n"
        "# File Counter\n\n"
        "Run: `bash -c 'ls -1 $1 | wc -l'`\n"
    )

    return tmp_path


@pytest.fixture
def pyclopse_home(skill_tree):
    """Temp pyclopse home directory with the skill tree."""
    return skill_tree


# ---------------------------------------------------------------------------
# registry unit tests
# ---------------------------------------------------------------------------

class TestSkillRegistry:

    def test_get_skill_dirs_no_home(self, tmp_path):
        dirs = get_skill_dirs(config_dir=str(tmp_path))
        assert dirs == []   # nothing exists yet

    def test_get_skill_dirs_global(self, tmp_path):
        (tmp_path / "skills").mkdir()
        dirs = get_skill_dirs(config_dir=str(tmp_path))
        assert len(dirs) == 1
        assert dirs[0] == tmp_path / "skills"

    def test_get_skill_dirs_agent(self, tmp_path):
        (tmp_path / "skills").mkdir()
        agent_skills = tmp_path / "agents" / "my-agent" / "skills"
        agent_skills.mkdir(parents=True)
        dirs = get_skill_dirs(agent_name="my-agent", config_dir=str(tmp_path))
        assert len(dirs) == 2

    def test_get_skill_dirs_extra(self, tmp_path):
        extra = tmp_path / "extra-skills"
        extra.mkdir()
        dirs = get_skill_dirs(config_dir=str(tmp_path), extra_dirs=[str(extra)])
        assert extra in dirs

    def test_discover_finds_skills(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        names = {s.name for s in skills}
        assert "hello-world" in names
        assert "file-counter" in names

    def test_discover_no_skills(self, tmp_path):
        skills = discover_skills(config_dir=str(tmp_path))
        assert skills == []

    def test_skill_info_fields(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        hw = next(s for s in skills if s.name == "hello-world")
        assert hw.version == "1.2.3"
        assert hw.description == "Prints hello world"
        assert hw.skill_md.exists()
        assert hw.path.is_dir()

    def test_skill_allowed_tools(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        fc = next(s for s in skills if s.name == "file-counter")
        assert fc.allowed_tools == ["bash"]

    def test_skill_dir_substitution(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        hw = next(s for s in skills if s.name == "hello-world")
        content = hw.read_content()
        # {skill_dir} should be replaced with the absolute path
        assert "{skill_dir}" not in content
        assert str(hw.path) in content

    def test_find_skill_found(self, pyclopse_home):
        skill = find_skill("hello-world", config_dir=str(pyclopse_home))
        assert skill is not None
        assert skill.name == "hello-world"

    def test_find_skill_case_insensitive(self, pyclopse_home):
        skill = find_skill("Hello-World", config_dir=str(pyclopse_home))
        assert skill is not None

    def test_find_skill_not_found(self, pyclopse_home):
        assert find_skill("nonexistent-xyz", config_dir=str(pyclopse_home)) is None

    def test_agent_skill_overrides_global(self, tmp_path):
        """Per-agent skill with same name overrides global skill."""
        global_skills = tmp_path / "skills" / "my-tool"
        global_skills.mkdir(parents=True)
        (global_skills / "SKILL.md").write_text(
            "---\nname: my-tool\ndescription: global version\n---\nbody\n"
        )
        agent_skills = tmp_path / "agents" / "bot" / "skills" / "my-tool"
        agent_skills.mkdir(parents=True)
        (agent_skills / "SKILL.md").write_text(
            "---\nname: my-tool\ndescription: agent version\n---\nbody\n"
        )
        skills = discover_skills(agent_name="bot", config_dir=str(tmp_path))
        tool = next(s for s in skills if s.name == "my-tool")
        assert tool.description == "agent version"

    def test_invalid_skill_missing_name_skipped(self, tmp_path):
        (tmp_path / "skills" / "bad-skill").mkdir(parents=True)
        (tmp_path / "skills" / "bad-skill" / "SKILL.md").write_text(
            "---\ndescription: no name field\n---\nbody\n"
        )
        skills = discover_skills(config_dir=str(tmp_path))
        names = {s.name for s in skills}
        assert "bad-skill" not in names


class TestFormatForPrompt:

    def test_empty_list(self):
        assert format_for_prompt([]) == ""

    def test_contains_skill_name(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        text = format_for_prompt(skills)
        assert "hello-world" in text
        assert "file-counter" in text

    def test_xml_structure(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        text = format_for_prompt(skills)
        assert "<available_skills>" in text
        assert "<skill>" in text
        assert "<name>" in text
        assert "<description>" in text
        assert "<location>" in text

    def test_custom_read_tool_name(self, pyclopse_home):
        skills = discover_skills(config_dir=str(pyclopse_home))
        text = format_for_prompt(skills, read_tool_name="my_skill_tool")
        assert "my_skill_tool" in text


# ---------------------------------------------------------------------------
# prompt_builder skill injection
# ---------------------------------------------------------------------------

class TestPromptBuilderSkillInjection:

    def test_skills_injected_in_prompt(self, tmp_path):
        """Skills are appended to system prompt when agent dir + skills dir exist."""
        from pyclopse.core.prompt_builder import build_system_prompt

        # Create agent dir with at least one file
        agent_dir = tmp_path / "agents" / "test-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "IDENTITY.md").write_text("# Identity\nI am test.")

        # Create a skill
        skill_dir = tmp_path / "skills" / "my-test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-test-skill\ndescription: A test skill\n---\nbody\n"
        )

        prompt = build_system_prompt("test-agent", config_dir=str(tmp_path))
        assert "my-test-skill" in prompt
        assert "<available_skills>" in prompt

    def test_no_skills_no_injection(self, tmp_path):
        from pyclopse.core.prompt_builder import build_system_prompt
        agent_dir = tmp_path / "agents" / "no-skill-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "IDENTITY.md").write_text("# Identity\nI am test.")
        prompt = build_system_prompt("no-skill-agent", config_dir=str(tmp_path))
        assert "<available_skills>" not in prompt

    def test_subagent_no_skill_injection(self, tmp_path):
        from pyclopse.core.prompt_builder import build_system_prompt
        agent_dir = tmp_path / "agents" / "sub"
        agent_dir.mkdir(parents=True)
        (agent_dir / "IDENTITY.md").write_text("# Identity\nI am sub.")
        skill_dir = tmp_path / "skills" / "some-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: some-skill\ndescription: A skill\n---\nbody\n"
        )
        prompt = build_system_prompt("sub", config_dir=str(tmp_path), is_subagent=True)
        assert "<available_skills>" not in prompt


# ---------------------------------------------------------------------------
# MCP tools: skills_list, skill_read
# ---------------------------------------------------------------------------

async def _pyclopse_mcp_session(home_dir: str):
    env = {
        **os.environ,
        "HOME": home_dir,
        "PYCLAW_MCP_TRANSPORT": "stdio",
        "PYCLAW_EXEC_SECURITY": "all",
    }
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "pyclopse.tools.server"],
        env=env,
    )


async def _call(session: ClientSession, tool: str, args: dict) -> str:
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


@pytest.mark.asyncio
async def test_mcp_skills_list_no_skills(tmp_path):
    """skills_list returns a 'no skills' message when nothing is installed."""
    params = await _pyclopse_mcp_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "skills_list", {})
            assert "No skills" in out or "skill" in out.lower()


@pytest.mark.asyncio
async def test_mcp_skills_list_with_skills(tmp_path):
    """skills_list finds and lists skills in HOME/.pyclopse/skills/."""
    pyclopse_dir = tmp_path / ".pyclopse" / "skills" / "demo-skill"
    pyclopse_dir.mkdir(parents=True)
    (pyclopse_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill for testing\n---\n## Demo\nDo the demo.\n"
    )
    params = await _pyclopse_mcp_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "skills_list", {})
            assert "demo-skill" in out
            assert "A demo skill for testing" in out


@pytest.mark.asyncio
async def test_mcp_skill_read(tmp_path):
    """skill_read returns full SKILL.md content with {skill_dir} substituted."""
    skill_dir = tmp_path / ".pyclopse" / "skills" / "reader-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reader-skill\ndescription: Test read skill\n---\n"
        "Run: `python {skill_dir}/main.py`\n"
    )
    (skill_dir / "main.py").write_text("print('hello')\n")

    params = await _pyclopse_mcp_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "skill_read", {"name": "reader-skill"})
            assert "reader-skill" in out
            assert "{skill_dir}" not in out   # substituted
            assert "main.py" in out


@pytest.mark.asyncio
async def test_mcp_skill_read_not_found(tmp_path):
    params = await _pyclopse_mcp_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "skill_read", {"name": "nonexistent-xyz"})
            assert "[ERROR]" in out
            assert "not found" in out.lower()


# ---------------------------------------------------------------------------
# /skills and /skill slash commands
# ---------------------------------------------------------------------------

class TestSkillCommands:
    """Unit tests for the /skills and /skill command handlers.

    We test the handlers directly without a full gateway, using a minimal stub.
    """

    def _make_ctx(self, pyclopse_home, agent_id="test"):
        from unittest.mock import MagicMock
        from pyclopse.core.commands import CommandContext

        session = MagicMock()
        session.id = "sess-001"
        session.agent_id = agent_id

        gw = MagicMock()
        gw._agent_manager = None

        ctx = CommandContext(
            gateway=gw,
            session=session,
            sender_id="user1",
            channel="test",
        )
        # Patch discover_skills to use our temp home
        return ctx

    @pytest.mark.asyncio
    async def test_cmd_skills_no_skills(self, tmp_path):
        """Test /skills command when no skills are installed."""
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        from unittest.mock import MagicMock, patch

        registry = CommandRegistry()
        gw = MagicMock()
        gw._agent_manager = None
        register_builtin_commands(registry, gw)

        from pyclopse.core.commands import CommandContext
        session = MagicMock()
        session.id = "s1"
        session.agent_id = "agent1"
        ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="test")

        with patch("pyclopse.skills.registry.get_skill_dirs", return_value=[]):
            result = await registry.dispatch("/skills", ctx)
        assert result is not None
        assert "No skills" in result or "skill" in result.lower()

    @pytest.mark.asyncio
    async def test_cmd_skills_lists_skills(self, pyclopse_home):
        """Test /skills command lists discovered skills."""
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands, CommandContext
        from unittest.mock import MagicMock, patch

        registry = CommandRegistry()
        gw = MagicMock()
        gw._agent_manager = None
        register_builtin_commands(registry, gw)

        session = MagicMock()
        session.id = "s1"
        session.agent_id = "agent1"
        ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="test")

        with patch(
            "pyclopse.skills.registry.get_skill_dirs",
            return_value=[pyclopse_home / "skills"],
        ):
            result = await registry.dispatch("/skills", ctx)

        assert result is not None
        assert "hello-world" in result
        assert "file-counter" in result

    @pytest.mark.asyncio
    async def test_cmd_skill_no_args(self, pyclopse_home):
        """Test /skill with no args returns usage."""
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands, CommandContext
        from unittest.mock import MagicMock

        registry = CommandRegistry()
        gw = MagicMock()
        gw._agent_manager = None
        register_builtin_commands(registry, gw)

        session = MagicMock()
        session.id = "s1"
        session.agent_id = "agent1"
        ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="test")

        result = await registry.dispatch("/skill", ctx)
        assert result is not None
        assert "Usage" in result or "usage" in result.lower()

    @pytest.mark.asyncio
    async def test_cmd_skill_not_found(self, pyclopse_home):
        """Test /skill with unknown name returns helpful error."""
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands, CommandContext
        from unittest.mock import MagicMock, patch

        registry = CommandRegistry()
        gw = MagicMock()
        gw._agent_manager = None
        register_builtin_commands(registry, gw)

        session = MagicMock()
        session.id = "s1"
        session.agent_id = "agent1"
        ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="test")

        with patch(
            "pyclopse.skills.registry.get_skill_dirs",
            return_value=[pyclopse_home / "skills"],
        ):
            result = await registry.dispatch("/skill nonexistent-xyz", ctx)

        assert result is not None
        assert "not found" in result.lower() or "[ERROR]" in result
