"""
Tests for Slack channel initialisation in Gateway._init_channels.

Covers:
  - Gateway._init_channels: creates _slack_web_client when Slack enabled+token set
  - Gateway._init_channels: no client when Slack disabled
  - Gateway._init_channels: no client when token missing
  - Gateway._init_channels: tolerates ImportError (slack-sdk not installed)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_minimal_gateway(slack_enabled=True, bot_token="xoxb-test"):
    """Build a bare-minimum Gateway stub for testing _init_channels Slack logic."""
    from pyclopse.core.gateway import Gateway
    from pyclopse.channels.slack_plugin import SlackChannelConfig
    from pyclopse.config.schema import (
        Config, ChannelsConfig, AgentsConfig, SecurityConfig, PluginsConfig,
    )

    gw = Gateway.__new__(Gateway)
    gw._initialized = False
    gw._is_running = False
    gw._logger = MagicMock()
    gw._channels = {}
    gw._slack_web_client = None
    gw._telegram_bot = None
    gw._telegram_chat_id = None

    slack_cfg = SlackChannelConfig(
        enabled=slack_enabled,
        bot_token=bot_token,
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

        with patch("pyclopse.channels.loader.load_all", return_value=[]):
            await gw._init_channels()

        assert gw._slack_web_client is None

    @pytest.mark.asyncio
    async def test_no_client_when_no_token(self):
        gw = _make_minimal_gateway(slack_enabled=True, bot_token=None)

        with patch("pyclopse.channels.loader.load_all", return_value=[]):
            await gw._init_channels()

        assert gw._slack_web_client is None

    @pytest.mark.asyncio
    async def test_tolerates_import_error_gracefully(self):
        """When slack_sdk is not installed, _init_channels must not raise."""
        gw = _make_minimal_gateway(slack_enabled=True, bot_token="xoxb-test")

        # slack_sdk is not installed in this test environment; _init_channels
        # should catch the ImportError and leave _slack_web_client as None.
        with patch("pyclopse.channels.loader.load_all", return_value=[]):
            await gw._init_channels()  # must not raise

        assert gw._slack_web_client is None
