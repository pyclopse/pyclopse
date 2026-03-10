"""Tests for GET /api/v1/tools (MCP tools catalog)."""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway_with_agents(agent_dicts):
    """Build a minimal gateway stub with agents config from raw dicts."""
    from pyclaw.core.gateway import Gateway
    from pyclaw.config.schema import Config, AgentsConfig, SecurityConfig

    gw = Gateway.__new__(Gateway)
    gw._initialized = True
    gw._is_running = True
    gw._logger = MagicMock()

    # Build AgentsConfig from raw dicts using model_validate with extra fields
    agents_cfg = AgentsConfig.model_validate(agent_dicts)
    gw._config = Config(agents=agents_cfg, security=SecurityConfig())
    return gw


def _make_app(gateway):
    from pyclaw.api.app import create_app, set_gateway
    set_gateway(gateway)
    return create_app(gateway)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestToolsEndpoint:

    def test_returns_agents_list(self):
        gw = _make_gateway_with_agents({
            "assistant": {
                "name": "My Assistant",
                "model": "openai/gpt-4",
            }
        })
        app = _make_app(gw)
        client = TestClient(app)
        resp = client.get("/api/v1/tools/")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "total_agents" in data

    def test_agent_mcp_servers_returned(self):
        gw = _make_gateway_with_agents({
            "coder": {
                "name": "Coder",
                "model": "claude-3-5-sonnet",
                "mcp_servers": ["filesystem", "github"],
            }
        })
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/tools/").json()
        agent = next(a for a in data["agents"] if a["agent_id"] == "coder")
        assert "filesystem" in agent["mcp_servers"]
        assert "github" in agent["mcp_servers"]

    def test_no_mcp_servers_returns_empty_list(self):
        gw = _make_gateway_with_agents({
            "basic": {
                "name": "Basic",
                "model": "openai/gpt-4",
            }
        })
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/tools/").json()
        agent = data["agents"][0]
        assert agent["mcp_servers"] == []

    def test_total_agents_count_correct(self):
        gw = _make_gateway_with_agents({
            "a1": {"name": "A1", "model": "gpt-4"},
            "a2": {"name": "A2", "model": "gpt-4"},
            "a3": {"name": "A3", "model": "gpt-4"},
        })
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/tools/").json()
        assert data["total_agents"] == 3

    def test_tools_profile_included(self):
        gw = _make_gateway_with_agents({
            "dev": {
                "name": "Dev",
                "model": "claude-3-5-sonnet",
                "tools": {"profile": "coding", "allow": ["bash"], "deny": []},
            }
        })
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/tools/").json()
        agent = data["agents"][0]
        assert agent["tools_profile"] == "coding"
        assert "bash" in agent["tools_allow"]
