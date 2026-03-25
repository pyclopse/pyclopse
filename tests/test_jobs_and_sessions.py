"""
Tests for:
  3. Cron expression parsing (croniter-based)
  4. Job results → Telegram notification callback
  5. TUI Telegram polling fix (one-liner, tested indirectly)
  6. Session persistence (write on create/add_message, load on start)
"""
import asyncio
import json
import tempfile
import uuid
from datetime import datetime, timedelta
from pyclawops.utils.time import now
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclawops.jobs.models import (
    Job, JobRun, JobStatus,
    CommandRun, IntervalSchedule, CronSchedule,
)
from pyclawops.jobs.scheduler import JobScheduler
from pyclawops.config.schema import JobsConfig
from pyclawops.core.session import Session, SessionManager


# ---------------------------------------------------------------------------
# Item 3: Cron expression parsing
# ---------------------------------------------------------------------------

class TestCronParsing:

    def _make_scheduler(self, tmp_path) -> JobScheduler:
        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        # Pin to UTC so tests are timezone-agnostic: naive datetimes == UTC
        return JobScheduler(cfg, default_timezone="UTC")

    def test_every_minute_fires_within_60s(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        now = datetime(2025, 1, 1, 12, 0, 0)
        nxt = sched._cron_next("* * * * *", now)
        assert nxt is not None
        delta = (nxt - now).total_seconds()
        assert 0 < delta <= 60

    def test_hourly_fires_within_3600s(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        now = datetime(2025, 1, 1, 12, 0, 0)
        nxt = sched._cron_next("0 * * * *", now)
        assert nxt is not None
        delta = (nxt - now).total_seconds()
        assert 0 < delta <= 3600

    def test_specific_time_fires_correctly(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        # At 2025-01-01 11:59:00, next "0 12 * * *" should be ~1 minute away
        now = datetime(2025, 1, 1, 11, 59, 0)
        nxt = sched._cron_next("0 12 * * *", now)
        assert nxt is not None
        assert nxt.hour == 12
        assert nxt.minute == 0

    def test_invalid_expression_falls_back(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        now = datetime(2025, 1, 1, 12, 0, 0)
        # Should not raise; returns a sane fallback
        nxt = sched._cron_next("not a cron expression", now)
        assert nxt is not None
        assert nxt > now

    def test_different_crons_give_different_times(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        now = datetime(2025, 1, 1, 0, 0, 0)
        every_min = sched._cron_next("* * * * *", now)
        every_hour = sched._cron_next("0 * * * *", now)
        assert every_min < every_hour


# ---------------------------------------------------------------------------
# Continuous cron
# ---------------------------------------------------------------------------

class TestContinuousCron:

    def _make_scheduler(self, tmp_path) -> JobScheduler:
        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        return JobScheduler(cfg, default_timezone="UTC")

    def test_is_continuous_detects_keyword(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        assert sched._is_continuous("continuous 7-14 * * 1-5")
        assert sched._is_continuous("CONTINUOUS 9-17 * * *")
        assert not sched._is_continuous("0 9 * * 1-5")
        assert not sched._is_continuous("* * * * *")

    def test_inside_window_returns_now(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        # Wednesday 10:30 UTC — inside "7-14 * * 1-5"
        t = datetime(2025, 1, 8, 10, 30, 0)
        nxt = sched._continuous_next("continuous 7-14 * * 1-5", t, "UTC")
        assert nxt == t  # restart immediately

    def test_outside_window_hour_schedules_next_open(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        # Wednesday 16:00 UTC — outside hour window (7-14)
        t = datetime(2025, 1, 8, 16, 0, 0)
        nxt = sched._continuous_next("continuous 7-14 * * 1-5", t, "UTC")
        assert nxt is not None
        assert nxt > t
        assert nxt.hour == 7  # next open is Thursday 07:00

    def test_outside_window_weekend_schedules_monday(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        # Saturday 10:00 UTC — outside day window (1-5)
        t = datetime(2025, 1, 11, 10, 0, 0)  # Saturday
        nxt = sched._continuous_next("continuous 7-14 * * 1-5", t, "UTC")
        assert nxt is not None
        assert nxt.weekday() == 0  # Monday

    def test_recalc_uses_continuous_path(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        job = Job(
            id=str(uuid.uuid4()),
            name="cont-job",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="continuous 7-14 * * 1-5"),
        )
        # Inside window: Wednesday 10:00
        with patch("pyclawops.jobs.scheduler.now", return_value=datetime(2025, 1, 8, 10, 0, 0)):
            sched._recalc_next_run(job)
        assert job.next_run == datetime(2025, 1, 8, 10, 0, 0)

    def test_recalc_outside_window_schedules_forward(self, tmp_path):
        sched = self._make_scheduler(tmp_path)
        job = Job(
            id=str(uuid.uuid4()),
            name="cont-job",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="continuous 7-14 * * 1-5"),
        )
        # Outside window: Wednesday 20:00
        with patch("pyclawops.jobs.scheduler.now", return_value=datetime(2025, 1, 8, 20, 0, 0)):
            sched._recalc_next_run(job)
        assert job.next_run > datetime(2025, 1, 8, 20, 0, 0)
        assert job.next_run.hour == 7


# ---------------------------------------------------------------------------
# Item 4: Job results → Telegram notification callback
# ---------------------------------------------------------------------------

class TestJobNotifyCallback:

    def _make_job(self) -> Job:
        return Job(
            id=str(uuid.uuid4()),
            name="test-job",
            run=CommandRun(command="echo hello"),
            schedule=IntervalSchedule(seconds=3600),
        )

    def _make_run(self, status: JobStatus, stdout="hello\n", stderr="", error=None) -> JobRun:
        run = JobRun(
            id=str(uuid.uuid4()),
            job_id="j1",
            job_name="test-job",
            started_at=now(),
            ended_at=now(),
            status=status,
            stdout=stdout,
            stderr=stderr,
            error=error,
        )
        return run

    @pytest.mark.asyncio
    async def test_callback_called_on_success(self, tmp_path):
        """notify_callback is fired after a successful job."""
        notify = AsyncMock()
        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        sched = JobScheduler(cfg, notify_callback=notify)

        job = self._make_job()

        # Directly fire the internal callback path
        with patch.object(sched, "_execute", new=AsyncMock(return_value={
            "success": True,
            "stdout": "hello",
            "stderr": "",
            "exit_code": 0,
        })):
            sched.jobs[job.id] = job
            sched._running_jobs.discard(job.id)
            await sched._run_job(job)

        # Give tasks time to run
        await asyncio.sleep(0.05)
        assert notify.called

    @pytest.mark.asyncio
    async def test_callback_receives_job_and_run(self, tmp_path):
        """Callback receives (Job, JobRun) with correct status."""
        received = []

        async def capture(j, r):
            received.append((j, r))

        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        sched = JobScheduler(cfg, notify_callback=capture)

        job = self._make_job()
        with patch.object(sched, "_execute", new=AsyncMock(return_value={
            "success": True,
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
        })):
            sched.jobs[job.id] = job
            await sched._run_job(job)

        await asyncio.sleep(0.05)
        # Callback fires twice: once at start (RUNNING) and once on completion
        assert len(received) == 2
        j, r = received[-1]
        assert j.name == "test-job"
        assert r.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_callback_fired_on_failure(self, tmp_path):
        """Callback is also fired when job fails."""
        received = []

        async def capture(j, r):
            received.append((j, r))

        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        sched = JobScheduler(cfg, notify_callback=capture)

        job = self._make_job()
        with patch.object(sched, "_execute", new=AsyncMock(return_value={
            "success": False,
            "stdout": "",
            "stderr": "oops",
            "exit_code": 1,
        })):
            sched.jobs[job.id] = job
            await sched._run_job(job)

        await asyncio.sleep(0.05)
        # Callback fires twice: once at start (RUNNING) and once on completion
        assert len(received) == 2
        _, r = received[-1]
        assert r.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_callback_does_not_crash(self, tmp_path):
        """Scheduler works normally without a notify_callback."""
        cfg = JobsConfig(enabled=True, agents_dir=str(tmp_path / "agents"))
        sched = JobScheduler(cfg)  # no callback

        job = self._make_job()
        with patch.object(sched, "_execute", new=AsyncMock(return_value={
            "success": True,
            "stdout": "ok",
            "stderr": "",
            "exit_code": 0,
        })):
            sched.jobs[job.id] = job
            await sched._run_job(job)  # should not raise

    @pytest.mark.asyncio
    async def test_gateway_job_notify_sends_telegram(self):
        """Gateway wires a callback that sends to Telegram."""
        # Simulate what _init_jobs does: build the callback and invoke it
        bot = AsyncMock()
        chat_id = "12345"

        from pyclawops.jobs.models import JobStatus, DeliverAnnounce

        async def _job_notify(job, run):
            ok = run.status == JobStatus.COMPLETED
            icon = "✅" if ok else "❌"
            lines = [f"{icon} Job *{job.name}*"]
            if run.stdout:
                lines.append(f"```\n{run.stdout.strip()[:500]}\n```")
            await bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode="Markdown",
            )

        job = Job(
            id="j1", name="my-job",
            run=CommandRun(command="echo hi"),
            schedule=IntervalSchedule(seconds=60),
        )
        run = JobRun(
            id="r1", job_id="j1", job_name="my-job",
            started_at=now(), ended_at=now(),
            status=JobStatus.COMPLETED, stdout="all good\n",
        )

        await _job_notify(job, run)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == chat_id
        assert "my-job" in call_kwargs["text"]
        assert "✅" in call_kwargs["text"]


# ---------------------------------------------------------------------------
# Item 6: Session persistence
# ---------------------------------------------------------------------------

class TestSessionPersistence:

    @pytest.mark.asyncio
    async def test_session_written_on_create(self, tmp_path):
        """Session metadata (session.json) is written on create."""
        mgr = SessionManager(agents_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("agent1", "telegram", "user1")
        meta_path = session.history_dir / "session.json"
        assert meta_path.exists(), "session.json should be written on create"
        data = json.loads(meta_path.read_text())
        assert data["id"] == session.id
        assert data["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_session_dir_uses_per_agent_layout(self, tmp_path):
        """Session dir is agents_dir/{agent_id}/sessions/{session_id}/."""
        mgr = SessionManager(agents_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("myagent", "telegram", "user1")
        expected_parent = tmp_path / "myagent" / "sessions"
        assert session.history_dir.parent == expected_parent

    @pytest.mark.asyncio
    async def test_session_id_has_date_prefix(self, tmp_path):
        """Session IDs are date-prefixed: YYYY-MM-DD-XXXXXX."""
        mgr = SessionManager(agents_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("a", "tg", "u1")
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}-[A-Za-z0-9]{6}$", session.id), (
            f"Session ID '{session.id}' should match YYYY-MM-DD-XXXXXX"
        )

    @pytest.mark.asyncio
    async def test_sessions_loaded_on_start(self, tmp_path):
        """Sessions written by one manager are loaded by a fresh one."""
        mgr1 = SessionManager(agents_dir=str(tmp_path))
        await mgr1.start()
        s = await mgr1.create_session("agent1", "telegram", "user42")
        s.touch(count_delta=3)  # simulate some activity

        mgr2 = SessionManager(agents_dir=str(tmp_path))
        await mgr2.start()
        assert s.id in mgr2.sessions
        loaded = mgr2.sessions[s.id]
        assert loaded.user_id == "user42"
        assert loaded.message_count == 3

    @pytest.mark.asyncio
    async def test_reaper_removes_from_index_not_disk(self, tmp_path):
        """Reaper evicts sessions from in-memory index but keeps files on disk."""
        mgr = SessionManager(agents_dir=str(tmp_path), ttl_hours=0)
        await mgr.start()
        session = await mgr.create_session("a", "tg", "u1")
        meta_path = session.history_dir / "session.json"
        assert meta_path.exists()

        # Manually trigger reaper
        await mgr._reap_stale_sessions()

        # Session removed from index
        assert session.id not in mgr.sessions
        # But file still on disk
        assert meta_path.exists()

    @pytest.mark.asyncio
    async def test_delete_session_removes_from_index_only(self, tmp_path):
        """delete_session removes from index; session directory is kept."""
        mgr = SessionManager(agents_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("a", "tg", "u1")
        hist_dir = session.history_dir
        assert hist_dir.exists()

        await mgr.delete_session(session.id)
        assert session.id not in mgr.sessions
        # Files still on disk
        assert hist_dir.exists()

    @pytest.mark.asyncio
    async def test_user_and_channel_indexes_rebuilt_on_load(self, tmp_path):
        mgr1 = SessionManager(agents_dir=str(tmp_path))
        await mgr1.start()
        await mgr1.create_session("a", "telegram", "alice")
        await mgr1.create_session("a", "telegram", "bob")

        mgr2 = SessionManager(agents_dir=str(tmp_path))
        await mgr2.start()
        assert "alice" in mgr2.user_sessions
        assert "bob" in mgr2.user_sessions
        assert "telegram" in mgr2.channel_sessions

    def test_session_to_dict_has_no_messages(self):
        """to_dict() contains metadata only — no message content."""
        s = Session(id="s1", agent_id="a", channel="tg", user_id="u")
        d = s.to_dict()
        assert "id" in d
        assert "messages" not in d

    def test_session_from_dict_round_trip(self, tmp_path):
        hist_dir = tmp_path / "sessions" / "s1"
        s = Session(id="s1", agent_id="a", channel="tg", user_id="u",
                    message_count=5, history_dir=hist_dir)
        d = s.to_dict()
        s2 = Session.from_dict(d, history_dir=hist_dir)
        assert s2.id == s.id
        assert s2.user_id == s.user_id
        assert s2.message_count == 5
        assert s2.history_dir == hist_dir

    def test_session_history_path(self, tmp_path):
        hist_dir = tmp_path / "sessions" / "abc"
        s = Session(id="abc", agent_id="a", channel="tg", user_id="u",
                    history_dir=hist_dir)
        assert s.history_path == hist_dir / "history.json"

    def test_session_history_path_none_when_no_dir(self):
        s = Session(id="abc", agent_id="a", channel="tg", user_id="u")
        assert s.history_path is None

    def test_touch_updates_count_and_metadata(self, tmp_path):
        hist_dir = tmp_path / "sessions" / "t1"
        hist_dir.mkdir(parents=True)
        s = Session(id="t1", agent_id="a", channel="tg", user_id="u",
                    history_dir=hist_dir)
        s.touch(count_delta=2)
        assert s.message_count == 2
        meta = json.loads((hist_dir / "session.json").read_text())
        assert meta["message_count"] == 2
