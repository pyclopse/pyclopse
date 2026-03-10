"""
Tests for the session reaper added to SessionManager.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from pyclaw.core.session import SessionManager
from pyclaw.config.schema import JobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_manager(tmp_path, ttl_hours=24, reaper_interval_minutes=60):
    mgr = SessionManager(
        persist_dir=str(tmp_path / "sessions"),
        ttl_hours=ttl_hours,
        reaper_interval_minutes=reaper_interval_minutes,
    )
    # Don't call start() — we invoke reaper manually to keep tests fast
    mgr._stop_event.clear()
    mgr._load_sessions_from_disk()
    return mgr


# ---------------------------------------------------------------------------
# Constructor / configuration
# ---------------------------------------------------------------------------

class TestSessionManagerConfig:

    def test_defaults(self, tmp_path):
        mgr = SessionManager(persist_dir=str(tmp_path))
        assert mgr.ttl_hours == 24
        assert mgr.reaper_interval_minutes == 60

    def test_custom_ttl(self, tmp_path):
        mgr = SessionManager(
            persist_dir=str(tmp_path), ttl_hours=48, reaper_interval_minutes=30
        )
        assert mgr.ttl_hours == 48
        assert mgr.reaper_interval_minutes == 30


# ---------------------------------------------------------------------------
# _reap_stale_sessions
# ---------------------------------------------------------------------------

class TestReapStaleSessions:

    @pytest.mark.asyncio
    async def test_fresh_session_not_reaped(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        session = await mgr.create_session("agent1", "telegram", "user1")
        # Session was just created — should not be reaped
        await mgr._reap_stale_sessions()
        assert session.id in mgr.sessions

    @pytest.mark.asyncio
    async def test_stale_session_is_reaped(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        session = await mgr.create_session("agent1", "telegram", "user1")
        # Artificially age the session beyond TTL
        session.updated_at = datetime.utcnow() - timedelta(hours=2)
        await mgr._reap_stale_sessions()
        assert session.id not in mgr.sessions

    @pytest.mark.asyncio
    async def test_reap_removes_persist_file(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        session = await mgr.create_session("agent1", "telegram", "user1")
        persist_path = mgr._session_path(session.id)
        assert persist_path and persist_path.exists()

        session.updated_at = datetime.utcnow() - timedelta(hours=2)
        await mgr._reap_stale_sessions()
        assert not persist_path.exists()

    @pytest.mark.asyncio
    async def test_multiple_stale_sessions_all_reaped(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        sessions = []
        for i in range(3):
            s = await mgr.create_session("agent1", "telegram", f"user{i}")
            s.updated_at = datetime.utcnow() - timedelta(hours=2)
            sessions.append(s)
        await mgr._reap_stale_sessions()
        for s in sessions:
            assert s.id not in mgr.sessions

    @pytest.mark.asyncio
    async def test_mix_fresh_and_stale(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        fresh = await mgr.create_session("agent1", "telegram", "fresh")
        stale = await mgr.create_session("agent1", "telegram", "stale")
        stale.updated_at = datetime.utcnow() - timedelta(hours=5)

        await mgr._reap_stale_sessions()
        assert fresh.id in mgr.sessions
        assert stale.id not in mgr.sessions

    @pytest.mark.asyncio
    async def test_reap_empty_sessions_no_error(self, tmp_path):
        mgr = await _make_manager(tmp_path, ttl_hours=1)
        # Should not raise
        await mgr._reap_stale_sessions()
        assert len(mgr.sessions) == 0


# ---------------------------------------------------------------------------
# Reaper task lifecycle
# ---------------------------------------------------------------------------

class TestReaperTaskLifecycle:

    @pytest.mark.asyncio
    async def test_start_creates_reaper_task(self, tmp_path):
        mgr = SessionManager(
            persist_dir=str(tmp_path / "sessions"),
            ttl_hours=1,
            reaper_interval_minutes=60,
        )
        await mgr.start()
        assert mgr._reaper_task is not None
        assert not mgr._reaper_task.done()
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_reaper_task(self, tmp_path):
        mgr = SessionManager(
            persist_dir=str(tmp_path / "sessions"),
            ttl_hours=1,
            reaper_interval_minutes=60,
        )
        await mgr.start()
        await mgr.stop()
        assert mgr._reaper_task is None or mgr._reaper_task.done()

    @pytest.mark.asyncio
    async def test_reaper_runs_on_interval(self, tmp_path):
        """Reaper task fires _reap_stale_sessions after the interval elapses."""
        mgr = SessionManager(
            persist_dir=str(tmp_path / "sessions"),
            ttl_hours=0,   # 0 hours → every session is immediately stale
            reaper_interval_minutes=0,  # interval=0 → fires immediately
        )
        mgr._stop_event.clear()
        mgr._load_sessions_from_disk()

        session = await mgr.create_session("agent1", "telegram", "user1")
        # Age it so it's stale
        session.updated_at = datetime.utcnow() - timedelta(seconds=1)

        # Run reaper loop for one iteration then stop
        async def _run_once():
            await asyncio.sleep(0)  # yield to let loop start
            await mgr._reap_stale_sessions()
            mgr._stop_event.set()

        await _run_once()
        assert session.id not in mgr.sessions
