"""
Tests for /api/v1/sessions HTTP endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id="sess-1",
    agent_id="agent1",
    channel="telegram",
    user_id="user1",
    message_count=3,
    is_active=True,
    messages=None,
):
    s = MagicMock()
    s.id = session_id
    s.agent_id = agent_id
    s.channel = channel
    s.user_id = user_id
    s.created_at = datetime(2025, 1, 1, 12, 0, 0)
    s.updated_at = datetime(2025, 1, 2, 8, 0, 0)
    s.message_count = message_count
    s.is_active = is_active
    s.messages = messages or []
    return s


def _make_message(msg_id="m1", role="user", content="hello"):
    m = MagicMock()
    m.id = msg_id
    m.role = role
    m.content = content
    m.timestamp = datetime(2025, 1, 1, 12, 0, 0)
    return m


def _make_app(session_manager):
    """Create a FastAPI app with a mock gateway wired to the given session_manager."""
    from pyclaw.api.app import create_app
    import pyclaw.api.app as _api_app

    gateway = MagicMock()
    gateway.session_manager = session_manager

    app = create_app(gateway=gateway)

    # Override the gateway for dependency resolution
    _api_app.set_gateway(gateway)
    return app


# ---------------------------------------------------------------------------
# GET /api/v1/sessions
# ---------------------------------------------------------------------------

class TestListSessions:

    @pytest.mark.asyncio
    async def test_list_returns_empty(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/sessions/")
        assert r.status_code == 200
        data = r.json()
        assert data["sessions"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_returns_sessions(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        await sm.create_session("agent1", "telegram", "user1")
        await sm.create_session("agent1", "telegram", "user2")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/sessions/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert len(data["sessions"]) == 2
        await sm.stop()

    @pytest.mark.asyncio
    async def test_list_filters_by_channel(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        await sm.create_session("a1", "telegram", "u1")
        await sm.create_session("a1", "slack", "u2")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/sessions/?channel=telegram")
        data = r.json()
        assert data["total"] == 1
        assert data["sessions"][0]["channel"] == "telegram"
        await sm.stop()

    @pytest.mark.asyncio
    async def test_list_session_fields(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        await sm.create_session("agent1", "telegram", "user99")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/sessions/")
        s = r.json()["sessions"][0]
        for key in ("id", "agent_id", "channel", "user_id", "created_at", "updated_at", "message_count", "is_active"):
            assert key in s, f"Missing field: {key}"
        await sm.stop()


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{id}
# ---------------------------------------------------------------------------

class TestGetSession:

    @pytest.mark.asyncio
    async def test_get_existing_session(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        session = await sm.create_session("agent1", "telegram", "user1")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/v1/sessions/{session.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == session.id
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"
        await sm.stop()

    @pytest.mark.asyncio
    async def test_get_missing_session_returns_404(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/sessions/nonexistent-id")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_session_message_fields(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        session = await sm.create_session("agent1", "telegram", "user1")
        session.add_message("user", "test message")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/v1/sessions/{session.id}")
        msg = r.json()["messages"][0]
        for key in ("id", "role", "content", "timestamp"):
            assert key in msg
        await sm.stop()


# ---------------------------------------------------------------------------
# DELETE /api/v1/sessions/{id}
# ---------------------------------------------------------------------------

class TestDeleteSession:

    @pytest.mark.asyncio
    async def test_delete_existing_session(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        session = await sm.create_session("agent1", "telegram", "user1")
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/api/v1/sessions/{session.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] is True
        assert data["session_id"] == session.id
        # Verify it's really gone
        assert session.id not in sm.sessions
        await sm.stop()

    @pytest.mark.asyncio
    async def test_delete_missing_session_returns_404(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete("/api/v1/sessions/does-not-exist")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_removes_persist_file(self, tmp_path):
        from pyclaw.core.session import SessionManager
        sm = SessionManager(persist_dir=str(tmp_path / "s"))
        await sm.start()
        session = await sm.create_session("agent1", "telegram", "user1")
        persist_path = sm._session_path(session.id)
        assert persist_path and persist_path.exists()
        app = _make_app(sm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.delete(f"/api/v1/sessions/{session.id}")
        assert not persist_path.exists()
        await sm.stop()


# ---------------------------------------------------------------------------
# Route registration smoke test
# ---------------------------------------------------------------------------

class TestRoutesRegistered:

    def test_sessions_routes_in_app(self):
        from pyclaw.api.app import create_app
        app = create_app()
        paths = {r.path for r in app.routes}
        assert any("/api/v1/sessions" in p for p in paths)
