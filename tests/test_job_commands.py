"""
Tests for:
  - /job Telegram command handler (_handle_job_command)
  - HTTP API app creation (smoke test)
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclaw.jobs.models import Job, JobStatus, CommandRun, CronSchedule, IntervalSchedule, DeliverAnnounce
from pyclaw.config.schema import JobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(tmp_path):
    from pyclaw.jobs.scheduler import JobScheduler
    cfg = JobsConfig(enabled=True, persist_file=str(tmp_path / "jobs.json"))
    return JobScheduler(cfg)


def _make_gateway_stub(tmp_path):
    """Minimal gateway-like object with _handle_job_command wired up."""
    from pyclaw.core.gateway import Gateway
    from unittest.mock import MagicMock

    gw = MagicMock(spec=Gateway)
    sched = _make_scheduler(tmp_path)
    gw._job_scheduler = sched
    # Bind the real method
    gw._handle_job_command = Gateway._handle_job_command.__get__(gw, Gateway)
    return gw, sched


# ---------------------------------------------------------------------------
# /job help
# ---------------------------------------------------------------------------

class TestJobCommandHelp:

    @pytest.mark.asyncio
    async def test_help_shown_for_unknown_subcommand(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job foobar")
        assert "help" in reply.lower()
        assert "/job add" in reply

    @pytest.mark.asyncio
    async def test_help_shown_for_bare_job_command(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job")
        assert "/job list" in reply

    @pytest.mark.asyncio
    async def test_help_explicit(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job help")
        assert "/job add" in reply
        assert "/job del" in reply
        assert "/job run" in reply

    @pytest.mark.asyncio
    async def test_no_scheduler_returns_message(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        gw._job_scheduler = None
        reply = await gw._handle_job_command("/job list")
        assert "not running" in reply.lower()


# ---------------------------------------------------------------------------
# /job list
# ---------------------------------------------------------------------------

class TestJobCommandList:

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job list")
        assert "No jobs" in reply

    @pytest.mark.asyncio
    async def test_list_shows_jobs(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        job = Job(
            id="abc12345", name="my-job",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="0 9 * * *"),
        )
        await sched.add_job(job)
        reply = await gw._handle_job_command("/job list")
        assert "abc12345" in reply
        assert "my-job" in reply

    @pytest.mark.asyncio
    async def test_list_shows_enabled_status(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        job = Job(
            id="j1", name="enabled-job",
            run=CommandRun(command="true"),
            schedule=CronSchedule(expr="* * * * *"),
            enabled=True,
        )
        await sched.add_job(job)
        reply = await gw._handle_job_command("/job list")
        assert "✅" in reply


# ---------------------------------------------------------------------------
# /job add
# ---------------------------------------------------------------------------

class TestJobCommandAdd:

    @pytest.mark.asyncio
    async def test_add_creates_job(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job add 0 9 * * * echo hello")
        assert "✅" in reply
        assert "echo hello" in reply
        jobs = await sched.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].run.command == "echo hello"
        assert jobs[0].schedule.expr == "0 9 * * *"

    @pytest.mark.asyncio
    async def test_add_quoted_cron(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command('/job add "0 9 * * 1-5" python script.py')
        assert "✅" in reply
        jobs = await sched.list_jobs()
        assert jobs[0].schedule.expr == "0 9 * * 1-5"
        assert jobs[0].run.command == "python script.py"

    @pytest.mark.asyncio
    async def test_add_rejects_invalid_cron(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job add not_a_cron_at_all echo hi")
        assert "Invalid" in reply or "invalid" in reply

    @pytest.mark.asyncio
    async def test_add_requires_command(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job add")
        assert "Usage" in reply or "usage" in reply


# ---------------------------------------------------------------------------
# /job del
# ---------------------------------------------------------------------------

class TestJobCommandDel:

    @pytest.mark.asyncio
    async def test_del_removes_job(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        job = Job(
            id="deadbeef", name="to-delete",
            run=CommandRun(command="echo bye"),
            schedule=IntervalSchedule(seconds=3600),
        )
        await sched.add_job(job)
        reply = await gw._handle_job_command("/job del deadbeef")
        assert "Deleted" in reply or "deleted" in reply.lower()
        jobs = await sched.list_jobs()
        assert len(jobs) == 0

    @pytest.mark.asyncio
    async def test_del_prefix_match(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        job = Job(
            id="cafebabe-1234", name="test",
            run=CommandRun(command="true"),
            schedule=IntervalSchedule(seconds=60),
        )
        await sched.add_job(job)
        reply = await gw._handle_job_command("/job del cafebabe")
        assert "Deleted" in reply or "deleted" in reply.lower()

    @pytest.mark.asyncio
    async def test_del_not_found(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job del xxxxxxxx")
        assert "No job found" in reply

    @pytest.mark.asyncio
    async def test_del_requires_id(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job del")
        assert "Usage" in reply or "usage" in reply


# ---------------------------------------------------------------------------
# /job run
# ---------------------------------------------------------------------------

class TestJobCommandRun:

    @pytest.mark.asyncio
    async def test_run_triggers_job(self, tmp_path):
        gw, sched = _make_gateway_stub(tmp_path)
        job = Job(
            id="runme12", name="run-me",
            run=CommandRun(command="echo triggered"),
            schedule=IntervalSchedule(seconds=3600),
        )
        await sched.add_job(job)
        with patch.object(sched, "run_job_now", new=AsyncMock(return_value=True)) as mock_run:
            reply = await gw._handle_job_command("/job run runme12")
            mock_run.assert_called_once_with("runme12")
        assert "Running" in reply or "running" in reply.lower()

    @pytest.mark.asyncio
    async def test_run_not_found(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job run xxxxxxxx")
        assert "No job found" in reply

    @pytest.mark.asyncio
    async def test_run_requires_id(self, tmp_path):
        gw, _ = _make_gateway_stub(tmp_path)
        reply = await gw._handle_job_command("/job run")
        assert "Usage" in reply or "usage" in reply


# ---------------------------------------------------------------------------
# HTTP API smoke test
# ---------------------------------------------------------------------------

class TestHttpApiSmoke:

    def test_create_app_returns_fastapi(self):
        from pyclaw.api.app import create_app
        from fastapi import FastAPI
        app = create_app()
        assert isinstance(app, FastAPI)

    def test_health_endpoint_registered(self):
        from pyclaw.api.app import create_app
        app = create_app()
        routes = {r.path for r in app.routes}
        assert "/health" in routes

    def test_api_routes_registered(self):
        from pyclaw.api.app import create_app
        app = create_app()
        paths = {r.path for r in app.routes}
        assert any("/api/v1/jobs" in p for p in paths)
        assert any("/api/v1/agents" in p for p in paths)

    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        from pyclaw.api.app import create_app
        from httpx import AsyncClient, ASGITransport
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"
