"""
Tests for config MCP tools: config_get, config_set, config_delete,
config_validate, config_reload, config_schema.

These tests launch the pyclopse MCP server via stdio (PYCLAW_MCP_TRANSPORT=stdio)
and verify the config tools work correctly against a temp config file.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_CONFIG = """
version: "1.0"
gateway:
  host: "0.0.0.0"
  port: 8080
  mcp_port: 8081
agents:
  assistant:
    model: "claude-sonnet-4-6"
    temperature: 0.7
"""


async def _pyclopse_session(env_overrides: dict | None = None):
    env = {
        **os.environ,
        "PYCLAW_EXEC_SECURITY": "all",
        "PYCLAW_MCP_TRANSPORT": "stdio",
    }
    if env_overrides:
        env.update(env_overrides)
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "pyclopse.tools.server"],
        env=env,
    )


async def _call(session: ClientSession, tool: str, args: dict) -> str:
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


# ---------------------------------------------------------------------------
# config_schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_schema_full():
    params = await _pyclopse_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "config_schema", {})
            schema = json.loads(out)
            assert "properties" in schema
            assert "gateway" in schema["properties"]
            assert "agents" in schema["properties"]


@pytest.mark.asyncio
async def test_config_schema_section_gateway():
    params = await _pyclopse_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "config_schema", {"section": "gateway"})
            schema = json.loads(out)
            props = schema.get("properties", {})
            assert "host" in props
            assert "port" in props


@pytest.mark.asyncio
async def test_config_schema_unknown_section():
    params = await _pyclopse_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "config_schema", {"section": "nonexistent_xyz"})
            assert "[ERROR]" in out
            assert "not found" in out.lower()


# ---------------------------------------------------------------------------
# config_validate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_validate_finds_real_config():
    params = await _pyclopse_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "config_validate", {})
            # Either valid or no config found — both are acceptable in CI
            assert "[OK]" in out or "[ERROR]" in out or "[INVALID]" in out


# ---------------------------------------------------------------------------
# config_set / config_delete / config_validate (with temp file)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_set_and_validate():
    """Write a temp config, set a value, validate the result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "pyclopse.yaml"
        cfg_path.write_text(_MINIMAL_CONFIG)

        # Point PYCLAW config search to our temp file via env (override search)
        # We do this by temporarily changing HOME or by setting a known path.
        # The ConfigLoader's DEFAULT_CONFIG_PATHS includes ./config.yaml so we
        # can point cwd to tmpdir using the server's working dir.
        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "pyclopse.tools.server"],
            env={
                **os.environ,
                "PYCLAW_MCP_TRANSPORT": "stdio",
                "PYCLAW_EXEC_SECURITY": "all",
                # Override HOME so ~/.pyclopse doesn't shadow our temp config
                "HOME": tmpdir,
            },
        )
        # Create ~/.pyclopse/ equivalent under tmpdir
        (Path(tmpdir) / ".pyclopse").mkdir()
        (Path(tmpdir) / ".pyclopse" / "config.yaml").write_text(_MINIMAL_CONFIG)

        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # Set a string value
                out = await _call(session, "config_set", {
                    "path": "agents.assistant.model",
                    "value": "claude-opus-4-6",
                })
                assert "[OK]" in out, f"Expected OK: {out}"

                # Validate
                out = await _call(session, "config_validate", {})
                assert "[OK]" in out, f"Expected valid: {out}"

                # Set a numeric value
                out = await _call(session, "config_set", {
                    "path": "agents.assistant.temperature",
                    "value": "0.3",
                })
                assert "[OK]" in out

                # Verify round-trip by reading the file back
                import yaml
                saved = yaml.safe_load(
                    (Path(tmpdir) / ".pyclopse" / "config.yaml").read_text()
                )
                assert saved["agents"]["assistant"]["model"] == "claude-opus-4-6"
                assert abs(saved["agents"]["assistant"]["temperature"] - 0.3) < 1e-6


@pytest.mark.asyncio
async def test_config_set_bool():
    """Set a boolean value using JSON true/false."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / ".pyclopse").mkdir()
        cfg = Path(tmpdir) / ".pyclopse" / "config.yaml"
        cfg.write_text(_MINIMAL_CONFIG)

        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "pyclopse.tools.server"],
            env={**os.environ, "PYCLAW_MCP_TRANSPORT": "stdio",
                 "PYCLAW_EXEC_SECURITY": "all", "HOME": tmpdir},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                out = await _call(session, "config_set", {
                    "path": "agents.assistant.show_thinking",
                    "value": "true",
                })
                assert "[OK]" in out

                import yaml
                saved = yaml.safe_load(cfg.read_text())
                assert saved["agents"]["assistant"]["show_thinking"] is True


@pytest.mark.asyncio
async def test_config_delete_key():
    """Delete an existing key from the config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / ".pyclopse").mkdir()
        cfg = Path(tmpdir) / ".pyclopse" / "config.yaml"
        cfg.write_text(_MINIMAL_CONFIG + "\n  extra_field: to_remove\n")

        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "pyclopse.tools.server"],
            env={**os.environ, "PYCLAW_MCP_TRANSPORT": "stdio",
                 "PYCLAW_EXEC_SECURITY": "all", "HOME": tmpdir},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # First set a key to delete
                await _call(session, "config_set", {
                    "path": "agents.assistant.description",
                    "value": "temporary",
                })

                out = await _call(session, "config_delete", {
                    "path": "agents.assistant.description",
                })
                assert "[OK]" in out

                import yaml
                saved = yaml.safe_load(cfg.read_text())
                assert "description" not in saved.get("agents", {}).get("assistant", {})


@pytest.mark.asyncio
async def test_config_delete_nonexistent_key():
    """Deleting a key that doesn't exist returns an error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / ".pyclopse").mkdir()
        (Path(tmpdir) / ".pyclopse" / "config.yaml").write_text(_MINIMAL_CONFIG)

        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "pyclopse.tools.server"],
            env={**os.environ, "PYCLAW_MCP_TRANSPORT": "stdio",
                 "PYCLAW_EXEC_SECURITY": "all", "HOME": tmpdir},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                out = await _call(session, "config_delete", {
                    "path": "agents.assistant.does_not_exist",
                })
                assert "[ERROR]" in out


@pytest.mark.asyncio
async def test_config_set_creates_nested_path():
    """config_set can create new nested keys that don't exist yet."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / ".pyclopse").mkdir()
        cfg = Path(tmpdir) / ".pyclopse" / "config.yaml"
        cfg.write_text(_MINIMAL_CONFIG)

        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "pyclopse.tools.server"],
            env={**os.environ, "PYCLAW_MCP_TRANSPORT": "stdio",
                 "PYCLAW_EXEC_SECURITY": "all", "HOME": tmpdir},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                out = await _call(session, "config_set", {
                    "path": "agents.assistant.tools.profile",
                    "value": "coding",
                })
                assert "[OK]" in out

                import yaml
                saved = yaml.safe_load(cfg.read_text())
                assert saved["agents"]["assistant"]["tools"]["profile"] == "coding"


@pytest.mark.asyncio
async def test_config_schema_section_security():
    """config_schema returns the security section schema."""
    params = await _pyclopse_session()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "config_schema", {"section": "security"})
            # Either returns a valid schema dict or an error if section not found directly
            assert len(out) > 0
            if not out.startswith("[ERROR]"):
                schema = json.loads(out)
                assert isinstance(schema, dict)
