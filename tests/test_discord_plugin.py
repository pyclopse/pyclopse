"""
Tests for DiscordPlugin — the unified Discord channel plugin.

Covers:
  - Access control (allowed/denied users, guild filtering)
  - Dedup
  - Non-streaming response flow
  - Command dispatch
  - Endpoint registration
  - Typing indicator
  - Outbound (send_message, edit_message, send_media, react, send_typing)
  - Bot message filtering
  - Stop lifecycle
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from pyclopse.channels.discord_plugin import DiscordPlugin, DiscordChannelConfig
from pyclopse.channels.base import MessageTarget, MediaAttachment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gw_handle(
    dispatch_return="agent reply",
    command_return=None,
    is_duplicate=False,
    check_access=True,
    agent_id="test_agent",
):
    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_return)
    handle.dispatch_command = AsyncMock(return_value=command_return)
    handle.is_duplicate = MagicMock(return_value=is_duplicate)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value=agent_id)
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    config = MagicMock()
    config.channels = MagicMock()
    type(handle).config = PropertyMock(return_value=config)
    return handle


def _make_discord_message(
    author_id=12345,
    author_name="Alice",
    channel_id=67890,
    guild_id=11111,
    text="hello",
    message_id=99999,
    is_bot=False,
    is_dm=False,
):
    msg = MagicMock()
    msg.author.id = author_id
    msg.author.name = author_name
    msg.author.display_name = author_name
    msg.author.bot = is_bot
    msg.content = text
    msg.id = message_id
    msg.channel.id = channel_id
    msg.channel.send = AsyncMock(return_value=MagicMock(id=100))
    msg.channel.trigger_typing = AsyncMock()
    msg.channel.fetch_message = AsyncMock()
    if is_dm:
        msg.guild = None
    else:
        msg.guild = MagicMock()
        msg.guild.id = guild_id
    return msg


def _make_plugin(
    check_access=True,
    is_duplicate=False,
    dispatch_return="agent reply",
    command_return=None,
    guilds=None,
    typing_indicator=True,
    dm_policy="open",
    group_policy="open",
    bot_user_id="999999",
):
    plugin = DiscordPlugin()
    mock_client = MagicMock()
    plugin._clients = {"_default": mock_client}
    plugin._bot_user_ids = {"_default": bot_user_id}
    plugin._config = DiscordChannelConfig(
        enabled=True,
        guilds=guilds or [],
        typing_indicator=typing_indicator,
        dmPolicy=dm_policy,
        groupPolicy=group_policy,
    )
    handle = _make_gw_handle(
        dispatch_return=dispatch_return,
        command_return=command_return,
        is_duplicate=is_duplicate,
        check_access=check_access,
    )
    plugin._gw = handle
    return plugin, handle


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    async def test_allowed_user_passes(self):
        plugin, handle = _make_plugin(check_access=True)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()

    async def test_denied_user_blocked(self):
        plugin, handle = _make_plugin(check_access=False)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Bot filtering
# ---------------------------------------------------------------------------

class TestBotFilter:

    async def test_bot_messages_ignored(self):
        plugin, handle = _make_plugin()
        msg = _make_discord_message(is_bot=True)
        await plugin._handle_message(msg)
        handle.dispatch.assert_not_called()

    async def test_empty_content_ignored(self):
        plugin, handle = _make_plugin()
        msg = _make_discord_message(text="")
        await plugin._handle_message(msg)
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Guild filtering
# ---------------------------------------------------------------------------

class TestGuildFilter:

    async def test_allowed_guild_passes(self):
        plugin, handle = _make_plugin(guilds=["11111"])
        msg = _make_discord_message(guild_id=11111)
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()

    async def test_unlisted_guild_dropped(self):
        plugin, handle = _make_plugin(guilds=["99999"])
        msg = _make_discord_message(guild_id=11111)
        await plugin._handle_message(msg)
        handle.dispatch.assert_not_called()

    async def test_empty_guild_list_allows_all(self):
        plugin, handle = _make_plugin(guilds=[])
        msg = _make_discord_message(guild_id=11111)
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()

    async def test_dm_passes_even_with_guild_filter(self):
        plugin, handle = _make_plugin(guilds=["99999"])
        msg = _make_discord_message(is_dm=True)
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDedup:

    async def test_duplicate_dropped(self):
        plugin, handle = _make_plugin(is_duplicate=True)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_not_called()

    async def test_non_duplicate_processed(self):
        plugin, handle = _make_plugin(is_duplicate=False)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

class TestMessageHandling:

    async def test_dispatch_called_and_response_sent(self):
        plugin, handle = _make_plugin(dispatch_return="Hello!")
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()
        msg.channel.send.assert_called()

    async def test_no_response_no_send(self):
        plugin, handle = _make_plugin(dispatch_return=None)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        handle.dispatch.assert_called_once()
        msg.channel.send.assert_not_called()

    async def test_dm_uses_author_id_as_session(self):
        plugin, handle = _make_plugin()
        msg = _make_discord_message(author_id=42, is_dm=True)
        await plugin._handle_message(msg)
        handle.register_endpoint.assert_called_once()
        ep = handle.register_endpoint.call_args[0][2]
        assert ep["sender_id"] == "42"

    async def test_guild_uses_channel_id_as_session(self):
        plugin, handle = _make_plugin()
        msg = _make_discord_message(author_id=42, channel_id=67890)
        await plugin._handle_message(msg)
        ep = handle.register_endpoint.call_args[0][2]
        assert ep["sender_id"] == "67890"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestCommandDispatch:

    async def test_slash_command_intercepted(self):
        plugin, handle = _make_plugin(command_return="Done!")
        msg = _make_discord_message(text="/help")
        await plugin._handle_message(msg)
        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_not_called()
        msg.channel.send.assert_called()

    async def test_unrecognized_command_falls_through(self):
        plugin, handle = _make_plugin(command_return=None)
        msg = _make_discord_message(text="/unknown")
        await plugin._handle_message(msg)
        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------

class TestTypingIndicator:

    async def test_typing_sent_when_enabled(self):
        plugin, handle = _make_plugin(typing_indicator=True)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        msg.channel.trigger_typing.assert_called()

    async def test_typing_not_sent_when_disabled(self):
        plugin, handle = _make_plugin(typing_indicator=False)
        msg = _make_discord_message()
        await plugin._handle_message(msg)
        msg.channel.trigger_typing.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

class TestOutbound:

    async def test_send_message(self):
        plugin = DiscordPlugin()
        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock(return_value=MagicMock(id=42))
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)
        plugin._clients = {"_default": mock_client}

        target = MessageTarget(channel="discord", user_id="12345")
        result = await plugin.send_message(target, "hello")
        mock_channel.send.assert_called_once_with("hello")
        assert result == "42"

    async def test_edit_message(self):
        plugin = DiscordPlugin()
        mock_msg = AsyncMock()
        mock_channel = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)
        plugin._clients = {"_default": mock_client}

        target = MessageTarget(channel="discord", user_id="12345")
        await plugin.edit_message(target, "99", "updated")
        mock_msg.edit.assert_called_once_with(content="updated")

    async def test_react(self):
        plugin = DiscordPlugin()
        mock_msg = MagicMock()
        mock_msg.add_reaction = AsyncMock()
        mock_channel = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)
        plugin._clients = {"_default": mock_client}

        target = MessageTarget(channel="discord", user_id="12345")
        await plugin.react(target, "99", "👍")
        mock_msg.add_reaction.assert_called_once_with("👍")

    async def test_send_typing(self):
        plugin = DiscordPlugin()
        mock_channel = AsyncMock()
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)
        plugin._clients = {"_default": mock_client}

        target = MessageTarget(channel="discord", user_id="12345")
        await plugin.send_typing(target)
        mock_channel.trigger_typing.assert_called_once()


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

class TestConfigSchema:

    def test_default_config(self):
        cfg = DiscordChannelConfig()
        assert cfg.enabled is True
        assert cfg.bot_token is None
        assert cfg.guilds == []
        assert cfg.allowed_users == []

    def test_from_yaml_dict(self):
        cfg = DiscordChannelConfig.model_validate({
            "enabled": True,
            "botToken": "my-token",
            "guilds": ["123", "456"],
            "allowedUsers": ["789"],
            "typingIndicator": False,
        })
        assert cfg.bot_token == "my-token"
        assert cfg.guilds == ["123", "456"]
        assert cfg.allowed_users == ["789"]
        assert cfg.typing_indicator is False

    def test_plugin_declares_schema(self):
        assert DiscordPlugin.config_schema is DiscordChannelConfig


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class TestStop:

    async def test_stop_closes_client(self):
        plugin = DiscordPlugin()
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        plugin._clients = {"_default": mock_client}

        async def _sleeper():
            await asyncio.sleep(999)

        plugin._client_tasks = {"_default": asyncio.create_task(_sleeper())}
        await plugin.stop()
        mock_client.close.assert_called_once()
        assert plugin._clients == {}
        assert plugin._client_tasks == {}

    async def test_stop_with_no_client(self):
        plugin = DiscordPlugin()
        await plugin.stop()  # Should not raise


# ---------------------------------------------------------------------------
# DM policy
# ---------------------------------------------------------------------------

class TestDmPolicy:

    async def test_open_allows_all_dms(self):
        plugin, handle = _make_plugin(dm_policy="open")
        msg = _make_discord_message(is_dm=True)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()

    async def test_closed_blocks_all_dms(self):
        plugin, handle = _make_plugin(dm_policy="closed")
        msg = _make_discord_message(is_dm=True)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_allowlist_blocks_unlisted_dm(self):
        plugin, handle = _make_plugin(dm_policy="allowlist")
        plugin._config.allowed_users = ["99999"]  # not the author
        msg = _make_discord_message(is_dm=True, author_id=12345)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_allowlist_allows_listed_dm(self):
        plugin, handle = _make_plugin(dm_policy="allowlist")
        plugin._config.allowed_users = ["12345"]
        msg = _make_discord_message(is_dm=True, author_id=12345)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Group policy
# ---------------------------------------------------------------------------

class TestGroupPolicy:

    async def test_open_allows_all_group_messages(self):
        plugin, handle = _make_plugin(group_policy="open")
        msg = _make_discord_message(is_dm=False)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()

    async def test_closed_blocks_all_group_messages(self):
        plugin, handle = _make_plugin(group_policy="closed")
        msg = _make_discord_message(is_dm=False)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_mention_ignores_without_mention(self):
        plugin, handle = _make_plugin(group_policy="mention", bot_user_id="999999")
        msg = _make_discord_message(is_dm=False, text="hello everyone")
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_mention_responds_when_mentioned(self):
        plugin, handle = _make_plugin(group_policy="mention", bot_user_id="999999")
        msg = _make_discord_message(is_dm=False, text="<@999999> what's up?")
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()
        # Verify mention was stripped from the text sent to agent
        call_kwargs = handle.dispatch.call_args.kwargs
        assert "<@999999>" not in call_kwargs["text"]
        assert "what's up?" in call_kwargs["text"]

    async def test_mention_strips_bang_format(self):
        plugin, handle = _make_plugin(group_policy="mention", bot_user_id="999999")
        msg = _make_discord_message(is_dm=False, text="<@!999999> hey")
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()

    async def test_mention_bare_mention_ignored(self):
        """A message that is ONLY the mention with no actual content."""
        plugin, handle = _make_plugin(group_policy="mention", bot_user_id="999999")
        msg = _make_discord_message(is_dm=False, text="<@999999>")
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_mention_policy_does_not_affect_dms(self):
        """DMs bypass group policy entirely."""
        plugin, handle = _make_plugin(group_policy="mention", dm_policy="open")
        msg = _make_discord_message(is_dm=True, text="hello")
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()

    async def test_allowlist_blocks_unlisted_group_user(self):
        plugin, handle = _make_plugin(group_policy="allowlist")
        plugin._config.allowed_users = ["99999"]
        msg = _make_discord_message(is_dm=False, author_id=12345)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Per-guild config
# ---------------------------------------------------------------------------

class TestPerGuildConfig:

    async def test_guild_override_group_policy(self):
        """Per-guild groupPolicy overrides top-level."""
        from pyclopse.channels.discord_plugin import DiscordGuildConfig
        plugin, handle = _make_plugin(group_policy="open")
        plugin._config.guild_config = {
            "11111": DiscordGuildConfig(groupPolicy="closed"),
        }
        msg = _make_discord_message(is_dm=False, guild_id=11111)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_not_called()

    async def test_channel_override_group_policy(self):
        """Per-channel groupPolicy overrides per-guild."""
        from pyclopse.channels.discord_plugin import DiscordGuildConfig, DiscordChannelAccessConfig
        plugin, handle = _make_plugin(group_policy="closed")
        plugin._config.guild_config = {
            "11111": DiscordGuildConfig(
                groupPolicy="closed",
                channels={
                    "67890": DiscordChannelAccessConfig(groupPolicy="open"),
                },
            ),
        }
        msg = _make_discord_message(is_dm=False, guild_id=11111, channel_id=67890)
        await plugin._handle_message(msg, "_default", plugin._clients["_default"])
        handle.dispatch.assert_called_once()
