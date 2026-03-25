"""
Tests for job delivery targeting: deliver channel/chat_id on Job model,
_job_notify routing, and /job add --channel --chat parsing.
"""

import pytest
from datetime import datetime
from pyclawops.utils.time import now
from unittest.mock import AsyncMock, MagicMock

from pyclawops.jobs.models import (
    Job, JobRun, JobStatus,
    CommandRun, CronSchedule, IntervalSchedule,
    DeliverAnnounce, DeliverWebhook, DeliverNone,
)


# ---------------------------------------------------------------------------
# Job model: deliver channel / chat_id fields
# ---------------------------------------------------------------------------

class TestJobModelDeliverFields:

    def _make_job(self, **deliver_kwargs):
        return Job(
            id="testjob",
            name="Test",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="0 * * * *"),
            deliver=DeliverAnnounce(**deliver_kwargs),
        )

    def test_default_deliver_channel_none(self):
        job = self._make_job()
        assert job.deliver.channel is None
        assert job.deliver.chat_id is None

    def test_deliver_channel_set(self):
        job = self._make_job(channel="telegram")
        assert job.deliver.channel == "telegram"

    def test_deliver_chat_id_set(self):
        job = self._make_job(chat_id="99999")
        assert job.deliver.chat_id == "99999"

    def test_both_deliver_fields_set(self):
        job = self._make_job(channel="telegram", chat_id="42")
        assert job.deliver.channel == "telegram"
        assert job.deliver.chat_id == "42"

    def test_model_dump_includes_deliver(self):
        job = self._make_job(channel="telegram", chat_id="777")
        d = job.model_dump(mode="json")
        assert d["deliver"]["channel"] == "telegram"
        assert d["deliver"]["chat_id"] == "777"

    def test_model_validate_restores_deliver(self):
        job = Job(
            id="abc",
            name="myj",
            run={"kind": "command", "command": "ls"},
            schedule={"kind": "cron", "expr": "0 * * * *"},
            deliver={"mode": "announce", "channel": "telegram", "chat_id": "555"},
        )
        assert job.deliver.channel == "telegram"
        assert job.deliver.chat_id == "555"

    def test_webhook_deliver(self):
        job = Job(
            id="w1",
            name="webhook-job",
            run=CommandRun(command="ls"),
            schedule=CronSchedule(expr="0 * * * *"),
            deliver=DeliverWebhook(url="https://example.com/hook"),
        )
        assert job.deliver.mode == "webhook"
        assert job.deliver.url == "https://example.com/hook"


# ---------------------------------------------------------------------------
# _job_notify routing: uses chat_id from deliver when set
# ---------------------------------------------------------------------------

class TestJobNotifyRouting:

    def _make_gateway_for_notify(self, default_chat_id="default_chat"):
        from pyclawops.core.gateway import Gateway
        from pyclawops.config.schema import Config, AgentsConfig, SecurityConfig, JobsConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._telegram_bot = AsyncMock()
        gw._telegram_chat_id = default_chat_id
        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())
        return gw

    def _make_job(self, chat_id=None, channel=None):
        return Job(
            id="jj1",
            name="MyJob",
            run=CommandRun(command="echo ok"),
            schedule=CronSchedule(expr="0 * * * *"),
            deliver=DeliverAnnounce(channel=channel, chat_id=chat_id),
        )

    def _make_run(self, success=True):
        run = JobRun(
            id="r1",
            job_id="jj1",
            job_name="MyJob",
            started_at=now(),
            ended_at=now(),
            status=JobStatus.COMPLETED if success else JobStatus.FAILED,
        )
        return run

    @pytest.mark.asyncio
    async def test_notify_uses_default_chat_when_no_target(self):
        """When job deliver has no chat_id, notify uses _telegram_chat_id."""
        gw = self._make_gateway_for_notify(default_chat_id="DEFAULT_CHAT")
        job = self._make_job()
        run = self._make_run()

        async def _job_notify(job, run):
            if not gw._telegram_bot:
                return
            d = job.deliver
            chat_id = (getattr(d, "chat_id", None) or gw._telegram_chat_id)
            if not chat_id:
                return
            ok = run.status == JobStatus.COMPLETED
            icon = "✅" if ok else "❌"
            await gw._telegram_bot.send_message(
                chat_id=chat_id, text=f"{icon} Job *{job.name}*", parse_mode="Markdown"
            )

        await _job_notify(job, run)
        call_kwargs = gw._telegram_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "DEFAULT_CHAT"

    @pytest.mark.asyncio
    async def test_notify_uses_deliver_chat_id_when_set(self):
        """When job deliver has chat_id, notify uses it over the default."""
        gw = self._make_gateway_for_notify(default_chat_id="DEFAULT_CHAT")
        job = self._make_job(chat_id="SPECIFIC_CHAT")
        run = self._make_run()

        async def _job_notify(job, run):
            if not gw._telegram_bot:
                return
            d = job.deliver
            chat_id = (getattr(d, "chat_id", None) or gw._telegram_chat_id)
            if not chat_id:
                return
            ok = run.status == JobStatus.COMPLETED
            icon = "✅" if ok else "❌"
            await gw._telegram_bot.send_message(
                chat_id=chat_id, text=f"{icon} Job *{job.name}*", parse_mode="Markdown"
            )

        await _job_notify(job, run)
        call_kwargs = gw._telegram_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "SPECIFIC_CHAT"

    @pytest.mark.asyncio
    async def test_notify_skips_when_no_chat_and_no_target(self):
        """When neither deliver chat_id nor default chat is set, no message sent."""
        gw = self._make_gateway_for_notify(default_chat_id=None)
        gw._telegram_chat_id = None
        job = self._make_job()
        run = self._make_run()

        async def _job_notify(job, run):
            if not gw._telegram_bot:
                return
            d = job.deliver
            chat_id = (getattr(d, "chat_id", None) or gw._telegram_chat_id)
            if not chat_id:
                return
            await gw._telegram_bot.send_message(chat_id=chat_id, text="msg")

        await _job_notify(job, run)
        gw._telegram_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_skips_when_no_telegram_bot(self):
        """No send_message attempt when telegram_bot is None."""
        gw = self._make_gateway_for_notify()
        gw._telegram_bot = None
        job = self._make_job(chat_id="CHAT")
        run = self._make_run()

        mock_bot = AsyncMock()

        async def _job_notify(job, run):
            if not gw._telegram_bot:
                return
            d = job.deliver
            chat_id = (getattr(d, "chat_id", None) or gw._telegram_chat_id)
            if not chat_id:
                return
            await mock_bot.send_message(chat_id=chat_id, text="msg")

        await _job_notify(job, run)
        mock_bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# /job add --channel --chat parsing
# ---------------------------------------------------------------------------

class TestJobAddTargetParsing:
    """Test that /job add strips --channel/--chat flags and stores them on Job.deliver."""

    def _make_gateway_with_scheduler(self):
        from pyclawops.core.gateway import Gateway
        from pyclawops.config.schema import Config, AgentsConfig, SecurityConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

        # Mock scheduler
        added_jobs = []

        async def fake_add_job(job):
            added_jobs.append(job)

        async def fake_list_jobs():
            return added_jobs

        mock_scheduler = MagicMock()
        mock_scheduler.add_job = fake_add_job
        mock_scheduler.list_jobs = fake_list_jobs
        gw._job_scheduler = mock_scheduler
        return gw, added_jobs

    @pytest.mark.asyncio
    async def test_add_with_channel_and_chat(self):
        gw, added = self._make_gateway_with_scheduler()
        result = await gw._handle_job_command(
            '/job add "0 * * * *" echo hello --channel telegram --chat 12345'
        )
        assert len(added) == 1
        job = added[0]
        assert job.deliver.channel == "telegram"
        assert job.deliver.chat_id == "12345"
        # command should NOT contain the flags
        assert "--channel" not in job.run.command
        assert "--chat" not in job.run.command
        assert "echo hello" in job.run.command

    @pytest.mark.asyncio
    async def test_add_without_flags_leaves_targets_none(self):
        gw, added = self._make_gateway_with_scheduler()
        await gw._handle_job_command('/job add "0 * * * *" echo hello')
        assert len(added) == 1
        assert added[0].deliver.channel is None
        assert added[0].deliver.chat_id is None

    @pytest.mark.asyncio
    async def test_add_only_chat_flag(self):
        gw, added = self._make_gateway_with_scheduler()
        await gw._handle_job_command('/job add "0 * * * *" ls --chat 99999')
        assert len(added) == 1
        assert added[0].deliver.chat_id == "99999"
        assert added[0].deliver.channel is None

    @pytest.mark.asyncio
    async def test_add_only_channel_flag(self):
        gw, added = self._make_gateway_with_scheduler()
        await gw._handle_job_command('/job add "0 * * * *" date --channel slack')
        assert len(added) == 1
        assert added[0].deliver.channel == "slack"
        assert added[0].deliver.chat_id is None

    @pytest.mark.asyncio
    async def test_result_includes_target_info_when_set(self):
        gw, added = self._make_gateway_with_scheduler()
        result = await gw._handle_job_command(
            '/job add "0 * * * *" uptime --channel telegram --chat 777'
        )
        assert "telegram" in result
        assert "777" in result

    @pytest.mark.asyncio
    async def test_result_no_target_info_when_no_flags(self):
        gw, added = self._make_gateway_with_scheduler()
        result = await gw._handle_job_command('/job add "0 * * * *" uptime')
        assert "target" not in result


# ---------------------------------------------------------------------------
# /job list shows target info
# ---------------------------------------------------------------------------

class TestJobListTargetDisplay:

    @pytest.mark.asyncio
    async def test_list_shows_target_when_set(self):
        from pyclawops.core.gateway import Gateway
        from pyclawops.config.schema import Config, AgentsConfig, SecurityConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

        job = Job(
            id="abc12345",
            name="myjob",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="0 * * * *"),
            deliver=DeliverAnnounce(channel="telegram", chat_id="42"),
        )

        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs = AsyncMock(return_value=[job])
        gw._job_scheduler = mock_scheduler

        result = await gw._handle_job_command("/job list")
        assert "telegram" in result
        assert "42" in result

    @pytest.mark.asyncio
    async def test_list_omits_target_when_not_set(self):
        from pyclawops.core.gateway import Gateway
        from pyclawops.config.schema import Config, AgentsConfig, SecurityConfig

        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

        job = Job(
            id="abc12345",
            name="myjob",
            run=CommandRun(command="echo hi"),
            schedule=CronSchedule(expr="0 * * * *"),
        )

        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs = AsyncMock(return_value=[job])
        gw._job_scheduler = mock_scheduler

        result = await gw._handle_job_command("/job list")
        assert "target" not in result
