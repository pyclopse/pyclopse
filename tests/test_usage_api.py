"""Tests for GET /api/v1/usage and usage counter wiring."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway(messages_total=0, by_agent=None, by_channel=None):
    from pyclopse.core.gateway import Gateway
    gw = Gateway.__new__(Gateway)
    gw._initialized = True
    gw._is_running = True
    gw._logger = MagicMock()
    gw._usage = {
        "messages_total": messages_total,
        "messages_by_agent": by_agent or {},
        "messages_by_channel": by_channel or {},
        "started_at": time.time() - 100,
    }
    return gw


def _make_app(gateway):
    from pyclopse.api.app import create_app, set_gateway
    set_gateway(gateway)
    return create_app(gateway)


# ---------------------------------------------------------------------------
# GET /api/v1/usage
# ---------------------------------------------------------------------------

class TestUsageEndpoint:

    def test_returns_messages_total(self):
        gw = _make_gateway(messages_total=42)
        app = _make_app(gw)
        client = TestClient(app)
        resp = client.get("/api/v1/usage/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages_total"] == 42

    def test_returns_by_agent(self):
        gw = _make_gateway(by_agent={"agent1": 10, "agent2": 5})
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/usage/").json()
        assert data["messages_by_agent"]["agent1"] == 10
        assert data["messages_by_agent"]["agent2"] == 5

    def test_returns_by_channel(self):
        gw = _make_gateway(by_channel={"telegram": 7, "api": 3})
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/usage/").json()
        assert data["messages_by_channel"]["telegram"] == 7

    def test_uptime_seconds_positive(self):
        gw = _make_gateway()
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/usage/").json()
        assert data["uptime_seconds"] >= 0

    def test_empty_counters_returns_zeros(self):
        gw = _make_gateway()
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/usage/").json()
        assert data["messages_total"] == 0
        assert data["messages_by_agent"] == {}
        assert data["messages_by_channel"] == {}


# ---------------------------------------------------------------------------
# Usage counter wiring in handle_message
# ---------------------------------------------------------------------------

class TestUsageCounterWiring:

    @pytest.mark.asyncio
    async def test_handle_message_increments_total(self):
        from pyclopse.core.gateway import Gateway
        from pyclopse.config.schema import Config, AgentsConfig, SecurityConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._audit_logger = None
        gw._hook_registry = None
        gw._known_session_ids = set()
        gw._usage = {
            "messages_total": 0,
            "messages_by_agent": {},
            "messages_by_channel": {},
            "started_at": time.time(),
        }

        # Stub session manager
        mock_session = MagicMock()
        mock_session.id = "s1"
        mock_session.agent_id = "myagent"
        mock_sm = MagicMock()
        mock_sm.get_active_session = AsyncMock(return_value=mock_session)
        mock_sm.create_session = AsyncMock(return_value=mock_session)
        mock_sm.set_active_session = MagicMock()
        gw._session_manager = mock_sm

        # Stub agent manager
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "hello"
        mock_agent.handle_message = AsyncMock(return_value=mock_response)
        mock_am = MagicMock()
        mock_am.agents = {"myagent": mock_agent}
        mock_am.get_agent = MagicMock(return_value=mock_agent)
        gw._agent_manager = mock_am

        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

        await gw.handle_message("telegram", "Alice", "123", "hi")

        assert gw._usage["messages_total"] == 1
        assert gw._usage["messages_by_agent"].get("myagent") == 1
        assert gw._usage["messages_by_channel"].get("telegram") == 1

    @pytest.mark.asyncio
    async def test_handle_message_accumulates_counts(self):
        from pyclopse.core.gateway import Gateway
        from pyclopse.config.schema import Config, AgentsConfig, SecurityConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._audit_logger = None
        gw._hook_registry = None
        gw._known_session_ids = set()
        gw._usage = {
            "messages_total": 5,
            "messages_by_agent": {"myagent": 3},
            "messages_by_channel": {"telegram": 5},
            "started_at": time.time(),
        }

        mock_session = MagicMock()
        mock_session.id = "s1"
        mock_session.agent_id = "myagent"
        mock_sm = MagicMock()
        mock_sm.get_active_session = AsyncMock(return_value=mock_session)
        mock_sm.create_session = AsyncMock(return_value=mock_session)
        mock_sm.set_active_session = MagicMock()
        gw._session_manager = mock_sm

        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "reply"
        mock_agent.handle_message = AsyncMock(return_value=mock_response)
        mock_am = MagicMock()
        mock_am.agents = {"myagent": mock_agent}
        mock_am.get_agent = MagicMock(return_value=mock_agent)
        gw._agent_manager = mock_am

        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

        await gw.handle_message("telegram", "Bob", "456", "bye")

        assert gw._usage["messages_total"] == 6
        assert gw._usage["messages_by_agent"]["myagent"] == 4
        assert gw._usage["messages_by_channel"]["telegram"] == 6
