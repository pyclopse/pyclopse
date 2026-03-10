"""
Tests for Slack pulse/heartbeat delivery.

Covers:
  - SlackConfig.pulse_channel field (schema)
  - Gateway._init_channels: creates _slack_web_client when Slack enabled+token set
  - Gateway._init_channels: no client when Slack disabled
  - Gateway._init_channels: no client when token missing
  - Gateway._init_channels: tolerates ImportError (slack-sdk not installed)
  - pulse_executor: sends to Slack when _slack_web_client + pulse_channel configured
  - pulse_executor: skips Slack when no pulse_channel configured
  - pulse_executor: skips Slack when no client
  - pulse_executor: Slack send failure is logged, not raised
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# SlackConfig schema
# ---------------------------------------------------------------------------

class TestSlackConfigPulseChannel:

    def test_pulse_channel_defaults_none(self):
        from pyclaw.config.schema import SlackConfig
        cfg = SlackConfig()
        assert cfg.pulse_channel is None

    def test_pulse_channel_snake_case(self):
        from pyclaw.config.schema import SlackConfig
        cfg = SlackConfig(pulse_channel="C1234567890")
        assert cfg.pulse_channel == "C1234567890"

    def test_pulse_channel_camel_case(self):
        from pyclaw.config.schema import SlackConfig
        cfg = SlackConfig.model_validate({"pulseChannel": "C9999999999"})
        assert cfg.pulse_channel == "C9999999999"

    def test_pulse_channel_in_full_config(self):
        from pyclaw.config.schema import Config
        cfg = Config.model_validate({
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-fake",
                    "pulseChannel": "C0000000001",
                }
            }
        })
        assert cfg.channels.slack.pulse_channel == "C0000000001"


# ---------------------------------------------------------------------------
# Helpers for gateway tests
# ---------------------------------------------------------------------------

def _make_minimal_gateway(slack_enabled=True, bot_token="xoxb-test", pulse_channel=None):
    """Build a bare-minimum Gateway stub for testing _init_channels Slack logic."""
    from pyclaw.core.gateway import Gateway
    from pyclaw.config.schema import (
        Config, ChannelsConfig, SlackConfig, AgentsConfig, SecurityConfig, PluginsConfig,
    )

    gw = Gateway.__new__(Gateway)
    gw._initialized = False
    gw._is_running = False
    gw._logger = MagicMock()
    gw._channels = {}
    gw._slack_web_client = None
    gw._telegram_bot = None
    gw._telegram_chat_id = None

    slack_cfg = SlackConfig(
        enabled=slack_enabled,
        bot_token=bot_token,
        pulse_channel=pulse_channel,
    )
    gw._config = Config(
        agents=AgentsConfig(),
        security=SecurityConfig(),
        plugins=PluginsConfig(),
        channels=ChannelsConfig(slack=slack_cfg),
    )
    return gw


# ---------------------------------------------------------------------------
# _init_channels: Slack client creation
# ---------------------------------------------------------------------------

class TestInitChannelsSlack:

    @pytest.mark.asyncio
    async def test_no_client_when_disabled(self):
        gw = _make_minimal_gateway(slack_enabled=False, bot_token="xoxb-test")

        with patch("pyclaw.channels.loader.load_all", return_value=[]):
            await gw._init_channels()

        assert gw._slack_web_client is None

    @pytest.mark.asyncio
    async def test_no_client_when_no_token(self):
        gw = _make_minimal_gateway(slack_enabled=True, bot_token=None)

        with patch("pyclaw.channels.loader.load_all", return_value=[]):
            await gw._init_channels()

        assert gw._slack_web_client is None

    @pytest.mark.asyncio
    async def test_tolerates_import_error_gracefully(self):
        """When slack_sdk is not installed, _init_channels must not raise."""
        gw = _make_minimal_gateway(slack_enabled=True, bot_token="xoxb-test")

        # slack_sdk is not installed in this test environment; _init_channels
        # should catch the ImportError and leave _slack_web_client as None.
        with patch("pyclaw.channels.loader.load_all", return_value=[]):
            await gw._init_channels()  # must not raise

        assert gw._slack_web_client is None


# ---------------------------------------------------------------------------
# pulse_executor: Slack delivery
# ---------------------------------------------------------------------------

def _make_pulse_gateway(pulse_channel=None, bot_token="xoxb-test"):
    """Build a gateway stub with full pulse executor context."""
    from pyclaw.core.gateway import Gateway
    from pyclaw.config.schema import (
        Config, ChannelsConfig, SlackConfig, AgentsConfig, SecurityConfig, PluginsConfig,
    )
    import time

    gw = Gateway.__new__(Gateway)
    gw._initialized = True
    gw._is_running = True
    gw._logger = MagicMock()
    gw._telegram_bot = None
    gw._telegram_chat_id = None
    gw._slack_web_client = None
    gw._last_pulse_result = None

    # Usage dict (required by some gateway internals)
    gw._usage = {
        "messages_total": 0,
        "messages_by_agent": {},
        "messages_by_channel": {},
        "started_at": time.time(),
    }

    slack_cfg = SlackConfig(
        enabled=True,
        bot_token=bot_token,
        pulse_channel=pulse_channel,
    )
    gw._config = Config(
        agents=AgentsConfig(),
        security=SecurityConfig(),
        plugins=PluginsConfig(),
        channels=ChannelsConfig(slack=slack_cfg),
    )

    # Agent manager stub
    mock_agent = MagicMock()
    mock_agent.run_heartbeat = AsyncMock(return_value="all good")
    mock_am = MagicMock()
    mock_am.get_agent.return_value = mock_agent
    gw._agent_manager = mock_am

    return gw


class TestPulseSlackDelivery:

    @pytest.mark.asyncio
    async def test_sends_to_slack_when_configured(self):
        gw = _make_pulse_gateway(pulse_channel="C1234567890")
        mock_slack = MagicMock()
        mock_slack.chat_postMessage = AsyncMock(return_value={"ok": True})
        gw._slack_web_client = mock_slack

        # Invoke _init_pulse to build the executor, then call it directly
        await gw._init_pulse()
        # Extract the executor from the PulseRunner
        runner = gw._pulse_runner
        executor = runner._agent_executor
        await executor("default", "check for updates")

        mock_slack.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C1234567890"
        assert "🫀" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_skips_slack_when_no_pulse_channel(self):
        gw = _make_pulse_gateway(pulse_channel=None)
        mock_slack = MagicMock()
        mock_slack.chat_postMessage = AsyncMock()
        gw._slack_web_client = mock_slack

        await gw._init_pulse()
        executor = gw._pulse_runner._agent_executor
        await executor("default", "check for updates")

        mock_slack.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_slack_when_no_client(self):
        gw = _make_pulse_gateway(pulse_channel="C1234567890")
        # _slack_web_client is None (already set by _make_pulse_gateway)
        assert gw._slack_web_client is None

        await gw._init_pulse()
        executor = gw._pulse_runner._agent_executor
        # Should not raise
        await executor("default", "check for updates")

    @pytest.mark.asyncio
    async def test_slack_send_failure_is_logged_not_raised(self):
        gw = _make_pulse_gateway(pulse_channel="C1234567890")
        mock_slack = MagicMock()
        mock_slack.chat_postMessage = AsyncMock(side_effect=Exception("network error"))
        gw._slack_web_client = mock_slack

        await gw._init_pulse()
        executor = gw._pulse_runner._agent_executor
        # Should not raise
        result = await executor("default", "check for updates")
        assert result  # some result was returned

        gw._logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_message_includes_agent_result(self):
        gw = _make_pulse_gateway(pulse_channel="C1234567890")
        mock_slack = MagicMock()
        mock_slack.chat_postMessage = AsyncMock(return_value={"ok": True})
        gw._slack_web_client = mock_slack

        await gw._init_pulse()
        executor = gw._pulse_runner._agent_executor
        await executor("default", "check for updates")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "all good" in text  # agent response is in the message
