"""Tests for GET /api/v1/health/detail (detailed health check)."""

import time
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway(initialized=True, running=True, sessions=3, agents=None):
    from pyclawops.core.gateway import Gateway
    from pyclawops.config.schema import Config, AgentsConfig, SecurityConfig

    gw = Gateway.__new__(Gateway)
    gw._initialized = initialized
    gw._is_running = running
    gw._logger = MagicMock()
    gw._audit_logger = MagicMock() if initialized else None
    gw._telegram_bot = None
    gw._job_scheduler = None

    gw._usage = {
        "messages_total": 0,
        "messages_by_agent": {},
        "messages_by_channel": {},
        "started_at": time.time() - 300,
    }
    gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

    # Session manager
    mock_sm = MagicMock()
    mock_session_list = [MagicMock() for _ in range(sessions)]
    mock_sm.list_sessions = MagicMock(return_value=mock_session_list)
    gw._session_manager = mock_sm

    # Agent manager
    agent_ids = agents or ["default"]
    mock_am = MagicMock()
    mock_am.agents = {a: MagicMock() for a in agent_ids}
    gw._agent_manager = mock_am

    return gw


def _make_app(gateway):
    from pyclawops.api.app import create_app, set_gateway
    set_gateway(gateway)
    return create_app(gateway)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealthDetailEndpoint:

    def test_returns_200(self):
        gw = _make_gateway()
        app = _make_app(gw)
        client = TestClient(app)
        resp = client.get("/api/v1/health/detail")
        assert resp.status_code == 200

    def test_status_healthy_when_initialized(self):
        gw = _make_gateway(initialized=True)
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["status"] == "healthy"

    def test_status_degraded_when_not_initialized(self):
        gw = _make_gateway(initialized=False)
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["status"] == "degraded"

    def test_initialized_field(self):
        gw = _make_gateway(initialized=True)
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["initialized"] is True

    def test_uptime_positive(self):
        gw = _make_gateway()
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["uptime_seconds"] > 0

    def test_subsystems_present(self):
        gw = _make_gateway()
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        subs = data["subsystems"]
        assert "session_manager" in subs
        assert "agent_manager" in subs

    def test_session_manager_shows_active_count(self):
        gw = _make_gateway(sessions=5)
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        sm = data["subsystems"]["session_manager"]
        assert sm["status"] == "ok"
        assert sm["active_sessions"] == 5

    def test_agent_manager_shows_agent_ids(self):
        gw = _make_gateway(agents=["alpha", "beta"])
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        am = data["subsystems"]["agent_manager"]
        assert am["status"] == "ok"
        assert "alpha" in am["agents"]
        assert "beta" in am["agents"]
        assert am["agent_count"] == 2

    def test_telegram_not_configured_when_bot_none(self):
        gw = _make_gateway()
        gw._telegram_bot = None
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["subsystems"]["telegram"]["status"] == "not_configured"

    def test_telegram_connected_when_bot_present(self):
        gw = _make_gateway()
        gw._telegram_bot = MagicMock()
        app = _make_app(gw)
        client = TestClient(app)
        data = client.get("/api/v1/health/detail").json()
        assert data["subsystems"]["telegram"]["status"] == "connected"
