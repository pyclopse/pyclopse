"""DiscordPlugin — unified channel plugin for Discord.

Event-driven via discord.py WebSocket gateway.  Supports multi-bot (one bot
per agent), DMs, guild text channels, threads, media, reactions, typing
indicators, DM/group policies, per-guild/per-channel config, and
mention-based activation.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from pyclopse.channels.base import MediaAttachment, MessageTarget
from pyclopse.channels.plugin import (
    ChannelCapabilities, ChannelConfig, ChannelPlugin, GatewayHandle,
)

_logger = logging.getLogger("pyclopse.channels.discord")

# Matches <@123456> or <@!123456> Discord mention syntax
_MENTION_RE = re.compile(r"<@!?(\d+)>")


# ---------------------------------------------------------------------------
# Discord-specific config
# ---------------------------------------------------------------------------

class DiscordChannelAccessConfig(BaseModel):
    """Per-guild or per-channel access override."""
    allowed_users: Optional[list] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[list] = Field(default=None, validation_alias="deniedUsers")
    group_policy: Optional[str] = Field(default=None, validation_alias="groupPolicy")
    """Override group_policy for this guild/channel: open | mention | allowlist | closed"""


class DiscordGuildConfig(DiscordChannelAccessConfig):
    """Per-guild config with optional per-channel overrides."""
    channels: Dict[str, DiscordChannelAccessConfig] = Field(default_factory=dict)


class DiscordBotConfig(BaseModel):
    """Per-bot Discord config within a multi-bot setup.

    Fields left as ``None`` inherit from the parent ``DiscordChannelConfig``.
    """
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    agent: Optional[str] = None
    allowed_users: Optional[list] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[list] = Field(default=None, validation_alias="deniedUsers")
    guilds: Optional[List[str]] = None
    typing_indicator: Optional[bool] = Field(default=None, validation_alias="typingIndicator")
    dm_policy: Optional[str] = Field(default=None, validation_alias="dmPolicy")
    group_policy: Optional[str] = Field(default=None, validation_alias="groupPolicy")
    guild_config: Optional[Dict[str, DiscordGuildConfig]] = Field(
        default=None, validation_alias="guildConfig",
    )


class DiscordChannelConfig(ChannelConfig):
    """Discord channel configuration — extends base with platform fields."""

    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    guilds: List[str] = Field(default_factory=list)
    """Guild IDs to listen on.  Empty = all guilds the bot is in."""
    bots: Dict[str, DiscordBotConfig] = Field(default_factory=dict)
    """Multi-bot: named bots, each routing to a specific agent."""

    dm_policy: str = Field(default="open", validation_alias="dmPolicy")
    """DM policy: open (anyone), allowlist (only allowed_users), closed (no DMs)."""

    group_policy: str = Field(default="mention", validation_alias="groupPolicy")
    """Group policy: open (respond to all), mention (only when @mentioned),
    allowlist (only allowed_users), closed (ignore all group messages)."""

    guild_config: Dict[str, DiscordGuildConfig] = Field(
        default_factory=dict, validation_alias="guildConfig",
    )
    """Per-guild config with optional per-channel overrides."""

    def effective_config_for_bot(self, name: str) -> DiscordBotConfig:
        """Return fully-resolved config for *name*, inheriting parent defaults."""
        bot = self.bots[name]
        return DiscordBotConfig.model_validate({
            "botToken": bot.bot_token,
            "agent": bot.agent,
            "allowedUsers": bot.allowed_users if bot.allowed_users is not None else self.allowed_users,
            "deniedUsers": bot.denied_users if bot.denied_users is not None else self.denied_users,
            "guilds": bot.guilds if bot.guilds is not None else self.guilds,
            "typingIndicator": bot.typing_indicator if bot.typing_indicator is not None else self.typing_indicator,
            "dmPolicy": bot.dm_policy if bot.dm_policy is not None else self.dm_policy,
            "groupPolicy": bot.group_policy if bot.group_policy is not None else self.group_policy,
            "guildConfig": bot.guild_config if bot.guild_config is not None else self.guild_config,
        })


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class DiscordPlugin(ChannelPlugin):
    """Discord channel plugin — multi-bot, event-driven via discord.py."""

    name = "discord"
    config_schema = DiscordChannelConfig
    capabilities = ChannelCapabilities(
        streaming=False,       # Discord edit rate limits too tight
        media=True,
        reactions=True,
        threads=True,
        typing_indicator=True,
        message_edit=True,
        html_formatting=False,  # Discord uses its own markdown
        max_message_length=2000,
    )

    def __init__(self) -> None:
        self._gw: Optional[GatewayHandle] = None
        self._clients: Dict[str, Any] = {}  # bot_name → discord.Client
        self._client_tasks: Dict[str, asyncio.Task] = {}
        self._config: Optional[DiscordChannelConfig] = None
        # bot_name → bot's own user ID (set on_ready, used for mention detection)
        self._bot_user_ids: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, gateway: GatewayHandle) -> None:
        self._gw = gateway
        self._config = self._load_config(gateway)

        if not self._config.enabled:
            _logger.info("Discord disabled or not configured")
            return

        try:
            import discord
        except ImportError:
            _logger.warning("discord.py not installed, Discord disabled")
            return

        bots_to_init = self._resolve_bots()
        if not bots_to_init:
            _logger.warning("Discord enabled but no bot tokens configured")
            return

        for bot_name, token, effective_cfg in bots_to_init:
            try:
                intents = discord.Intents.default()
                intents.message_content = True
                intents.guilds = True
                intents.dm_messages = True

                client = discord.Client(intents=intents)

                _bn = bot_name
                _cfg = effective_cfg

                @client.event
                async def on_ready(*, _name=_bn, _client=client) -> None:
                    self._bot_user_ids[_name] = str(_client.user.id)
                    _logger.info(
                        f"Discord bot '{_name}' ready: {_client.user} "
                        f"(agent={_cfg.agent or 'first'}, guilds: {len(_client.guilds)})"
                    )

                @client.event
                async def on_message(message: Any, *, _name=_bn, _client=client) -> None:
                    asyncio.create_task(self._handle_message(message, _name, _client))

                self._clients[bot_name] = client
                task = asyncio.create_task(
                    client.start(token),
                    name=f"discord-{bot_name}",
                )
                self._client_tasks[bot_name] = task
                _logger.info(
                    f"Discord bot '{bot_name}' starting "
                    f"(agent={effective_cfg.agent or 'first'}, "
                    f"dm_policy={getattr(effective_cfg, 'dm_policy', 'open')}, "
                    f"group_policy={getattr(effective_cfg, 'group_policy', 'mention')})"
                )
            except Exception as e:
                _logger.error(f"Failed to start Discord bot '{bot_name}': {e}")

    async def stop(self) -> None:
        for bot_name, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception:
                pass
        for bot_name, task in list(self._client_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._client_tasks.clear()
        self._clients.clear()
        self._bot_user_ids.clear()

    # ── Outbound (gateway / fan-out calls these) ─────────────────────────

    async def send_message(
        self,
        target: MessageTarget,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return None
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return None
        try:
            channel = await self._resolve_channel(client, channel_id)
            if not channel:
                return None
            msg = await channel.send(text)
            return str(msg.id)
        except Exception as e:
            _logger.error(f"send_message failed: {e}")
            return None

    async def edit_message(
        self,
        target: MessageTarget,
        message_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return
        try:
            channel = await self._resolve_channel(client, channel_id)
            if not channel:
                return
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=text)
        except Exception as e:
            if "unknown message" not in str(e).lower():
                _logger.error(f"edit_message failed: {e}")

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
        **kwargs: Any,
    ) -> Optional[str]:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return None
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return None
        try:
            import discord
            channel = await self._resolve_channel(client, channel_id)
            if not channel:
                return None
            if media.file_path:
                file = discord.File(media.file_path)
                msg = await channel.send(content=media.caption, file=file)
            elif media.url:
                embed = discord.Embed()
                mime = (media.mime_type or "").lower()
                if mime.startswith("image/"):
                    embed.set_image(url=media.url)
                else:
                    embed.description = media.url
                if media.caption:
                    embed.title = media.caption
                msg = await channel.send(embed=embed)
            else:
                return None
            return str(msg.id)
        except Exception as e:
            _logger.error(f"send_media failed: {e}")
            return None

    async def react(
        self,
        target: MessageTarget,
        message_id: str,
        emoji: str,
    ) -> None:
        client = self._resolve_client()
        if not client:
            return
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return
        try:
            channel = await self._resolve_channel(client, channel_id)
            if channel:
                msg = await channel.fetch_message(int(message_id))
                await msg.add_reaction(emoji)
        except Exception as e:
            _logger.debug(f"react failed: {e}")

    async def send_typing(self, target: MessageTarget) -> None:
        client = self._resolve_client()
        if not client:
            return
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return
        try:
            channel = await self._resolve_channel(client, channel_id)
            if channel:
                await channel.trigger_typing()
        except Exception:
            pass

    # ── Inbound message handler ──────────────────────────────────────────

    async def _handle_message(
        self,
        message: Any,
        bot_name: str = "_default",
        client: Optional[Any] = None,
    ) -> None:
        """Route one incoming Discord message to the agent and reply."""
        client = client or self._resolve_client()
        if not self._gw or not client:
            return

        # Ignore bot messages (including our own)
        if message.author.bot:
            return

        user_id = str(message.author.id)
        text = message.content or ""
        if not text:
            return

        effective_cfg = self._effective_config(bot_name)
        is_dm = message.guild is None
        guild_id = str(message.guild.id) if message.guild else None
        channel_id = str(message.channel.id)

        # ── Policy checks ────────────────────────────────────────────────
        if is_dm:
            policy = self._resolve_dm_policy(bot_name, effective_cfg)
            if policy == "closed":
                return
            if policy == "allowlist":
                allowed = getattr(effective_cfg, "allowed_users", []) or []
                if allowed and user_id not in [str(u) for u in allowed]:
                    _logger.debug(f"DM from {user_id} blocked by allowlist policy (bot={bot_name})")
                    return
        else:
            policy = self._resolve_group_policy(bot_name, effective_cfg, guild_id, channel_id)
            if policy == "closed":
                return
            if policy == "mention":
                bot_uid = self._bot_user_ids.get(bot_name)
                if bot_uid and not self._is_mentioned(text, bot_uid):
                    return  # Silently ignore — not mentioned
                # Strip the mention from the text so the agent doesn't see it
                text = self._strip_mentions(text, self._bot_user_ids.get(bot_name))
                if not text.strip():
                    return  # Just a bare mention, nothing to process
            if policy == "allowlist":
                allowed = self._resolve_allowed_users(effective_cfg, guild_id, channel_id)
                if allowed and user_id not in [str(u) for u in allowed]:
                    _logger.debug(f"Group message from {user_id} blocked by allowlist (bot={bot_name})")
                    return

        # Guild filter (per-bot — separate from group_policy)
        bot_guilds = getattr(effective_cfg, "guilds", []) or []
        if bot_guilds and guild_id and guild_id not in bot_guilds:
            return

        # Dedup
        msg_id = f"{bot_name}/{guild_id or 'dm'}/{channel_id}/{message.id}"
        if self._gw.is_duplicate("discord", msg_id):
            return

        # Access control (global deny/allow — on top of policy checks)
        allowed = getattr(effective_cfg, "allowed_users", []) or []
        denied = getattr(effective_cfg, "denied_users", []) or []
        if not self._gw.check_access(user_id, allowed, denied):
            _logger.debug(f"Ignored message from unauthorized user {user_id} (bot={bot_name})")
            return

        sender_name = message.author.display_name or message.author.name or user_id
        agent_id = self._agent_id_for_bot(bot_name)

        _logger.info(
            f"Discord message received: bot={bot_name} user={user_id} "
            f"agent={agent_id} {'DM' if is_dm else f'guild={guild_id}'} text={text[:60]!r}"
        )

        # Slash command interception
        if text.strip().startswith("/"):
            thread_id = str(message.channel.id) if hasattr(message.channel, "thread") else None
            reply = await self._gw.dispatch_command(
                channel="discord",
                user_id=user_id,
                text=text.strip(),
                thread_id=thread_id,
                agent_id=agent_id,
            )
            if reply is not None:
                try:
                    for chunk in self._gw.split_message(reply, 2000):
                        await message.channel.send(chunk)
                except Exception as e:
                    _logger.error(f"Failed to send command reply: {e}")
                return

        # Session key: user ID for DMs, channel ID for guilds
        session_id = user_id if is_dm else channel_id

        # Register endpoint (with bot_name for fan-out)
        self._gw.register_endpoint(agent_id, "discord", {
            "sender_id": session_id,
            "sender": sender_name,
            "bot_name": bot_name,
        })

        # Typing indicator
        typing_enabled = getattr(effective_cfg, "typing_indicator", True)
        if typing_enabled:
            try:
                await message.channel.trigger_typing()
            except Exception:
                pass

        try:
            response = await self._gw.dispatch(
                channel="discord",
                user_id=user_id,
                user_name=sender_name,
                text=text,
                message_id=msg_id,
                agent_id=agent_id,
            )
            if response:
                from pyclopse.agents.runner import strip_thinking_tags
                clean = strip_thinking_tags(response)
                for chunk in self._gw.split_message(clean, 2000):
                    await message.channel.send(chunk)
        except asyncio.CancelledError:
            _logger.info(f"Discord message cancelled for {user_id}")
        except Exception as e:
            _logger.error(f"Error handling Discord message from {user_id} (bot={bot_name}): {e}")
            try:
                await message.channel.send(f"Sorry, I hit an error: {e}")
            except Exception:
                pass

    # ── Policy resolution ─────────────────────────────────────────────────

    def _resolve_dm_policy(self, bot_name: str, effective_cfg: Any) -> str:
        """Resolve DM policy: open | allowlist | closed."""
        return getattr(effective_cfg, "dm_policy", None) or "open"

    def _resolve_group_policy(
        self, bot_name: str, effective_cfg: Any,
        guild_id: Optional[str], channel_id: Optional[str],
    ) -> str:
        """Resolve group policy with per-guild/per-channel overrides."""
        guild_cfg = self._get_guild_config(effective_cfg, guild_id)
        # Per-channel override takes highest priority
        if guild_cfg and channel_id:
            ch_cfg = guild_cfg.channels.get(channel_id)
            if ch_cfg and ch_cfg.group_policy:
                return ch_cfg.group_policy
        # Per-guild override
        if guild_cfg and guild_cfg.group_policy:
            return guild_cfg.group_policy
        # Bot-level / top-level default
        return getattr(effective_cfg, "group_policy", None) or "mention"

    def _resolve_allowed_users(
        self, effective_cfg: Any,
        guild_id: Optional[str], channel_id: Optional[str],
    ) -> list:
        """Resolve allowed_users with per-guild/per-channel overrides."""
        guild_cfg = self._get_guild_config(effective_cfg, guild_id)
        if guild_cfg and channel_id:
            ch_cfg = guild_cfg.channels.get(channel_id)
            if ch_cfg and ch_cfg.allowed_users is not None:
                return ch_cfg.allowed_users
        if guild_cfg and guild_cfg.allowed_users is not None:
            return guild_cfg.allowed_users
        return getattr(effective_cfg, "allowed_users", []) or []

    def _get_guild_config(self, effective_cfg: Any, guild_id: Optional[str]) -> Optional[DiscordGuildConfig]:
        """Look up per-guild config."""
        if not guild_id:
            return None
        gc = getattr(effective_cfg, "guild_config", None)
        if gc and isinstance(gc, dict):
            return gc.get(guild_id)
        return None

    def _is_mentioned(self, text: str, bot_user_id: Optional[str]) -> bool:
        """Check if the bot is @mentioned in the message."""
        if not bot_user_id:
            return False
        mentions = _MENTION_RE.findall(text)
        return bot_user_id in mentions

    def _strip_mentions(self, text: str, bot_user_id: Optional[str]) -> str:
        """Strip bot @mentions from text so the agent sees clean input."""
        if not bot_user_id:
            return text
        return re.sub(rf"<@!?{re.escape(bot_user_id)}>\s*", "", text).strip()

    # ── Bot resolution helpers ────────────────────────────────────────────

    def _resolve_bots(self) -> List[Tuple[str, str, Any]]:
        """Build (bot_name, token, effective_config) list."""
        cfg = self._config
        if not cfg:
            return []
        result: List[Tuple[str, str, Any]] = []
        if cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.bot_token:
                    result.append((bot_name, effective.bot_token, effective))
                else:
                    _logger.warning(f"Discord bot '{bot_name}' has no botToken, skipping")
        elif cfg.bot_token:
            result.append(("_default", cfg.bot_token, cfg))
        return result

    def _resolve_client(self, bot_name: Optional[str] = None) -> Optional[Any]:
        """Get a discord.Client by name, or first available."""
        if bot_name and bot_name in self._clients:
            return self._clients[bot_name]
        return next(iter(self._clients.values()), None)

    def _effective_config(self, bot_name: str) -> Any:
        """Return effective per-bot config (falls back to parent)."""
        cfg = self._config
        if not cfg:
            return None
        if cfg.bots and bot_name in cfg.bots:
            return cfg.effective_config_for_bot(bot_name)
        return cfg

    def _agent_id_for_bot(self, bot_name: str) -> str:
        """Resolve which agent handles this bot's messages."""
        cfg = self._config
        if cfg and cfg.bots and bot_name in cfg.bots:
            effective = cfg.effective_config_for_bot(bot_name)
            if effective.agent:
                return self._gw.resolve_agent_id(effective.agent)
        return self._gw.resolve_agent_id()

    def bot_for_agent(self, agent_id: str) -> Tuple[Optional[Any], Optional[str]]:
        """Return (client, bot_name) for the bot configured for *agent_id*."""
        cfg = self._config
        if cfg and cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.agent == agent_id and bot_name in self._clients:
                    return self._clients[bot_name], bot_name
        if self._clients:
            bot_name = next(iter(self._clients))
            return self._clients[bot_name], bot_name
        return None, None

    async def _resolve_channel(self, client: Any, channel_id: str) -> Optional[Any]:
        """Resolve a channel ID to a discord.py channel object."""
        if not client:
            return None
        int_id = int(channel_id)
        channel = client.get_channel(int_id)
        if channel:
            return channel
        try:
            return await client.fetch_channel(int_id)
        except Exception:
            pass
        try:
            user = await client.fetch_user(int_id)
            return await user.create_dm()
        except Exception:
            _logger.debug(f"Could not resolve channel or user for ID {channel_id}")
            return None
