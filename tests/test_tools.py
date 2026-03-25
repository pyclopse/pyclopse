"""
Comprehensive tests for pyclawops tool system.

Tests:
  - pyclawops MCP server (bash, web_search, send_message, sessions_*, memory_*, session_status)
  - External MCP servers (time, fetch, filesystem)
  - Tool policy engine (ToolPolicy)
  - Security enforcement (allowlist, blocked patterns)
  - Full MCP protocol via mcp.ClientSession
"""
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pyclawops.tools.policy import ToolGroup, ToolPolicy, TOOL_GROUPS, TOOL_PROFILES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call(session: ClientSession, tool: str, args: dict) -> str:
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


async def _pyclawops_session(env_overrides: dict | None = None):
    """Context manager: open a pyclawops MCP server session via stdio."""
    env = {
        **os.environ,
        "PYCLAW_EXEC_SECURITY": "all",
        "PYCLAW_EXEC_TIMEOUT": "10",
        "PYCLAW_MCP_TRANSPORT": "stdio",   # force stdio for test subprocess
    }
    if env_overrides:
        env.update(env_overrides)
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "pyclawops.tools.server"],
        env=env,
    )
    return params


# ---------------------------------------------------------------------------
# ToolPolicy unit tests (no I/O)
# ---------------------------------------------------------------------------

class TestToolPolicy:

    def test_default_is_full(self):
        policy = ToolPolicy()
        assert "bash" in policy.allowed
        assert "read_file" in policy.allowed
        assert "web_search" in policy.allowed

    def test_explicit_allowlist(self):
        policy = ToolPolicy({"allow": ["bash", "read_file"]})
        assert policy.is_allowed("bash")
        assert policy.is_allowed("read_file")
        assert not policy.is_allowed("web_search")

    def test_profile_coding(self):
        policy = ToolPolicy({"profile": "coding"})
        assert policy.is_allowed("bash")
        assert policy.is_allowed("read_file")
        assert not policy.is_allowed("send_message")

    def test_profile_web(self):
        policy = ToolPolicy({"profile": "web"})
        assert policy.is_allowed("web_search")
        assert policy.is_allowed("web_fetch")
        assert not policy.is_allowed("bash")

    def test_profile_plus_allow(self):
        policy = ToolPolicy({"profile": "coding", "allow": ["send_message"]})
        assert policy.is_allowed("bash")
        assert policy.is_allowed("send_message")

    def test_profile_minus_deny(self):
        policy = ToolPolicy({"profile": "coding", "deny": ["bash"]})
        assert not policy.is_allowed("bash")
        assert policy.is_allowed("read_file")

    def test_group_expansion(self):
        policy = ToolPolicy({"allow": ["group:fs"]})
        for tool in TOOL_GROUPS[ToolGroup.FS]:
            assert policy.is_allowed(tool)
        assert not policy.is_allowed("bash")

    def test_filter_tools(self):
        policy = ToolPolicy({"allow": ["bash", "read_file"]})
        filtered = policy.filter_tools(["bash", "read_file", "web_search", "send_message"])
        assert filtered == ["bash", "read_file"]

    def test_profile_minimal(self):
        policy = ToolPolicy({"profile": "minimal"})
        assert policy.is_allowed("session_status")
        assert not policy.is_allowed("bash")
        assert not policy.is_allowed("read_file")

    def test_profile_full(self):
        policy = ToolPolicy({"profile": "full"})
        assert policy.is_allowed("bash")
        assert policy.is_allowed("web_search")
        assert policy.is_allowed("send_message")


# ---------------------------------------------------------------------------
# pyclawops MCP server: tool listing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pyclawops_server_lists_tools():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            expected = {
                "bash", "web_search", "send_message",
                "sessions_list", "sessions_history",
                "memory_search", "memory_get", "session_status",
            }
            assert expected.issubset(names), f"Missing tools: {expected - names}"


# ---------------------------------------------------------------------------
# pyclawops MCP server: bash tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bash_echo():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "echo 'hello pyclawops'"})
            assert "hello pyclawops" in out


@pytest.mark.asyncio
async def test_bash_multiline():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {
                "command": "for i in 1 2 3; do echo $i; done"
            })
            assert "1" in out and "2" in out and "3" in out


@pytest.mark.asyncio
async def test_bash_exit_code():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "exit 42"})
            assert "[exit 42]" in out


@pytest.mark.asyncio
async def test_bash_stderr():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "echo error_msg >&2"})
            assert "error_msg" in out


@pytest.mark.asyncio
async def test_bash_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        params = await _pyclawops_session()
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                out = await _call(session, "bash", {"command": "pwd", "cwd": tmp})
                # macOS: /tmp resolves to /private/tmp
                assert tmp.replace("/tmp/", "/private/tmp/") in out or tmp in out


@pytest.mark.asyncio
async def test_bash_timeout():
    params = await _pyclawops_session({"PYCLAW_EXEC_TIMEOUT": "2"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "sleep 60", "timeout": 2})
            assert "[TIMEOUT]" in out


@pytest.mark.asyncio
async def test_bash_background():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {
                "command": "sleep 2", "background": True
            })
            assert "[BACKGROUND]" in out
            assert "PID=" in out


# ---------------------------------------------------------------------------
# pyclawops MCP server: bash security
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bash_blocked_rm_rf():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "rm -rf /"})
            assert "[DENIED]" in out


@pytest.mark.asyncio
async def test_bash_allowlist_blocks_unlisted():
    params = await _pyclawops_session({
        "PYCLAW_EXEC_SECURITY": "allowlist",
        "PYCLAW_SAFE_BINS": "echo,ls",
    })
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            # echo is allowed
            out = await _call(session, "bash", {"command": "echo hi"})
            assert "hi" in out
            # curl is NOT in safe_bins
            out = await _call(session, "bash", {"command": "curl http://example.com"})
            assert "[DENIED]" in out


@pytest.mark.asyncio
async def test_bash_none_security_blocks_all():
    params = await _pyclawops_session({"PYCLAW_EXEC_SECURITY": "none"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "bash", {"command": "echo hi"})
            assert "[DENIED]" in out


# ---------------------------------------------------------------------------
# pyclawops MCP server: web_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_web_search_returns_results():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "web_search", {
                "query": "python programming language", "max_results": 3
            })
            assert "Search results for" in out or "python" in out.lower()
            # Should have at least one URL
            assert "http" in out.lower()


@pytest.mark.asyncio
async def test_web_search_max_results():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "web_search", {
                "query": "openai chatgpt", "max_results": 1
            })
            # Should get at most 1 result separator
            assert out.count("---") <= 1


# ---------------------------------------------------------------------------
# pyclawops MCP server: sessions tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sessions_list_no_dir():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "sessions_list", {})
            # Either lists sessions or says none found
            assert "session" in out.lower() or "No sessions" in out


@pytest.mark.asyncio
async def test_sessions_history_not_found():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "sessions_history", {
                "session_id": "nonexistent-session-xyz"
            })
            assert "No session found" in out or "Error" in out


# ---------------------------------------------------------------------------
# pyclawops MCP server: session_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_status():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "session_status", {})
            # Either running gateway or unavailable message
            assert "status" in out.lower() or "unavailable" in out.lower()


# ---------------------------------------------------------------------------
# pyclawops MCP server: memory tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_search_returns_result():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "memory_search", {"query": "test query"})
            assert len(out) > 0


@pytest.mark.asyncio
async def test_memory_get_returns_result():
    params = await _pyclawops_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "memory_get", {"key": "test-key"})
            assert len(out) > 0


# ---------------------------------------------------------------------------
# External MCP server: time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_server_tools():
    params = StdioServerParameters(command="uvx", args=["mcp-server-time"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "get_current_time" in names
            assert "convert_time" in names


@pytest.mark.asyncio
async def test_time_get_current():
    params = StdioServerParameters(command="uvx", args=["mcp-server-time"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "get_current_time", {"timezone": "UTC"})
            assert "UTC" in out or "timezone" in out.lower()
            # Should contain a time/date
            assert "2026" in out or "202" in out


@pytest.mark.asyncio
async def test_time_convert():
    params = StdioServerParameters(command="uvx", args=["mcp-server-time"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "convert_time", {
                "source_timezone": "UTC",
                "time": "12:00",
                "target_timezone": "America/New_York",
            })
            assert len(out) > 0


# ---------------------------------------------------------------------------
# External MCP server: fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_server_tools():
    params = StdioServerParameters(command="uvx", args=["mcp-server-fetch"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "fetch" in names


# ---------------------------------------------------------------------------
# External MCP server: filesystem
# ---------------------------------------------------------------------------

def _real(path: str) -> str:
    """Resolve macOS symlinks (/var -> /private/var, /tmp -> /private/tmp)."""
    return os.path.realpath(path)


@pytest.mark.asyncio
async def test_filesystem_server_tools():
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _real(tmp)
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", real_tmp],
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "read_file" in names
                assert "write_file" in names
                assert "list_directory" in names


@pytest.mark.asyncio
async def test_filesystem_write_read():
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _real(tmp)
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", real_tmp],
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                test_file = str(Path(real_tmp) / "test.txt")
                await session.call_tool("write_file", {
                    "path": test_file,
                    "content": "Hello from pyclawops tests!",
                })

                out = await _call(session, "read_file", {"path": test_file})
                assert "Hello from pyclawops tests!" in out

                out = await _call(session, "list_directory", {"path": real_tmp})
                assert "test.txt" in out


@pytest.mark.asyncio
async def test_filesystem_path_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _real(tmp)
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", real_tmp],
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool("read_file", {"path": "/etc/passwd"})
                text = result.content[0].text if result.content else ""
                assert "denied" in text.lower() or "not allowed" in text.lower() or "outside" in text.lower()
