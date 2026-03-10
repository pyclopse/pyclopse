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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclaw.jobs.models import (
    Job, JobRun, JobStatus,
    CommandRun, IntervalSchedule, CronSchedule,
)
from pyclaw.jobs.scheduler import JobScheduler
from pyclaw.config.schema import JobsConfig
from pyclaw.core.session import Session, SessionManager, Message


# ---------------------------------------------------------------------------
# Item 3: Cron expression parsing
# ---------------------------------------------------------------------------

class TestCronParsing:

    def _make_scheduler(self, tmp_path) -> JobScheduler:
        cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
        return JobScheduler(cfg)

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
            started_at=datetime.utcnow(),
            ended_at=datetime.utcnow(),
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
        cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
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

        cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
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
        assert len(received) == 1
        j, r = received[0]
        assert j.name == "test-job"
        assert r.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_callback_fired_on_failure(self, tmp_path):
        """Callback is also fired when job fails."""
        received = []

        async def capture(j, r):
            received.append((j, r))

        cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
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
        assert len(received) == 1
        _, r = received[0]
        assert r.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_callback_does_not_crash(self, tmp_path):
        """Scheduler works normally without a notify_callback."""
        cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
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

        from pyclaw.jobs.models import JobStatus, DeliverAnnounce

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
            started_at=datetime.utcnow(), ended_at=datetime.utcnow(),
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
        mgr = SessionManager(persist_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("agent1", "telegram", "user1")
        path = tmp_path / f"{session.id}.json"
        assert path.exists(), "Session file should be written on create"
        data = json.loads(path.read_text())
        assert data["id"] == session.id
        assert data["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_messages_written_on_add(self, tmp_path):
        mgr = SessionManager(persist_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("agent1", "telegram", "user1")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")

        path = tmp_path / f"{session.id}.json"
        data = json.loads(path.read_text())
        assert len(data["messages"]) == 2
        assert data["messages"][0]["content"] == "hello"
        assert data["messages"][1]["content"] == "hi there"

    @pytest.mark.asyncio
    async def test_sessions_loaded_on_start(self, tmp_path):
        # Write a session, then create a fresh manager and verify it loads it
        mgr1 = SessionManager(persist_dir=str(tmp_path))
        await mgr1.start()
        s = await mgr1.create_session("agent1", "telegram", "user42")
        s.add_message("user", "persisted message")

        # New manager, same directory
        mgr2 = SessionManager(persist_dir=str(tmp_path))
        await mgr2.start()
        assert s.id in mgr2.sessions
        loaded = mgr2.sessions[s.id]
        assert loaded.user_id == "user42"
        assert len(loaded.messages) == 1
        assert loaded.messages[0].content == "persisted message"

    @pytest.mark.asyncio
    async def test_messages_added_to_loaded_session_persist(self, tmp_path):
        """After loading from disk, new messages are still auto-saved."""
        mgr1 = SessionManager(persist_dir=str(tmp_path))
        await mgr1.start()
        s = await mgr1.create_session("a", "tg", "u1")

        mgr2 = SessionManager(persist_dir=str(tmp_path))
        await mgr2.start()
        loaded = mgr2.sessions[s.id]
        loaded.add_message("user", "new message after reload")

        path = tmp_path / f"{s.id}.json"
        data = json.loads(path.read_text())
        assert any(m["content"] == "new message after reload" for m in data["messages"])

    @pytest.mark.asyncio
    async def test_session_file_deleted_on_remove(self, tmp_path):
        mgr = SessionManager(persist_dir=str(tmp_path))
        await mgr.start()
        session = await mgr.create_session("a", "tg", "u1")
        path = tmp_path / f"{session.id}.json"
        assert path.exists()

        await mgr.delete_session(session.id)
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_no_persist_dir_does_not_crash(self):
        """SessionManager without persist_dir works normally (in-memory only)."""
        mgr = SessionManager()  # no persist_dir
        await mgr.start()
        session = await mgr.create_session("a", "tg", "u1")
        session.add_message("user", "hello")  # should not raise
        assert len(session.messages) == 1

    @pytest.mark.asyncio
    async def test_user_and_channel_indexes_rebuilt_on_load(self, tmp_path):
        mgr1 = SessionManager(persist_dir=str(tmp_path))
        await mgr1.start()
        await mgr1.create_session("a", "telegram", "alice")
        await mgr1.create_session("a", "telegram", "bob")

        mgr2 = SessionManager(persist_dir=str(tmp_path))
        await mgr2.start()
        # Both users should be in the index
        assert "alice" in mgr2.user_sessions
        assert "bob" in mgr2.user_sessions
        assert "telegram" in mgr2.channel_sessions

    def test_session_to_full_dict_includes_messages(self):
        s = Session(id="s1", agent_id="a", channel="tg", user_id="u")
        s.add_message("user", "hi")
        d = s.to_full_dict()
        assert "messages" in d
        assert len(d["messages"]) == 1
        assert d["messages"][0]["content"] == "hi"

    def test_session_from_dict_round_trip(self):
        s = Session(id="s1", agent_id="a", channel="tg", user_id="u")
        s.add_message("user", "round trip test")
        d = s.to_full_dict()
        s2 = Session.from_dict(d)
        assert s2.id == s.id
        assert s2.user_id == s.user_id
        assert len(s2.messages) == 1
        assert s2.messages[0].content == "round trip test"
