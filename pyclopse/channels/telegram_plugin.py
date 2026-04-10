"""TelegramPlugin — unified channel plugin for Telegram.

Replaces the legacy hard-coded Telegram integration in gateway.py with a
self-contained :class:`ChannelPlugin` subclass.  Supports multi-bot, streaming
(edit-in-place), thinking formatting, typing indicators, and cross-channel
fan-out.
"""

import asyncio
import html as _html
import logging
import re as _re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from pyclopse.channels.base import MediaAttachment, MessageTarget
from pyclopse.channels.plugin import (
    ChannelCapabilities, ChannelConfig, ChannelPlugin, GatewayHandle,
)


_OPEN_THINK = _re.compile(r"<(thinking|think)>", _re.IGNORECASE)

_logger = logging.getLogger("pyclopse.channels.telegram")


# ---------------------------------------------------------------------------
# Telegram-specific config (plugin-declared schema)
# ---------------------------------------------------------------------------

class TelegramBotConfig(BaseModel):
    """Per-bot Telegram config within a multi-bot setup.

    Fields left as ``None`` inherit the value from the parent config.
    """
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    agent: Optional[str] = None
    allowed_users: Optional[list] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[list] = Field(default=None, validation_alias="deniedUsers")
    typing_indicator: Optional[bool] = Field(default=None, validation_alias="typingIndicator")
    streaming: Optional[bool] = None


class TelegramChannelConfig(ChannelConfig):
    """Telegram channel configuration — extends base with platform fields."""

    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    topics: Dict[str, int] = Field(default_factory=dict)
    bots: Dict[str, TelegramBotConfig] = Field(default_factory=dict)

    def effective_config_for_bot(self, name: str) -> TelegramBotConfig:
        """Return fully-resolved config for *name*, inheriting parent defaults."""
        bot = self.bots[name]
        return TelegramBotConfig.model_validate({
            "botToken": bot.bot_token,
            "agent": bot.agent,
            "allowedUsers": bot.allowed_users if bot.allowed_users is not None else self.allowed_users,
            "deniedUsers": bot.denied_users if bot.denied_users is not None else self.denied_users,
            "typingIndicator": bot.typing_indicator if bot.typing_indicator is not None else self.typing_indicator,
            "streaming": bot.streaming if bot.streaming is not None else self.streaming,
        })


class TelegramPlugin(ChannelPlugin):
    """Telegram channel plugin — multi-bot, streaming, thinking formatting."""

    name = "telegram"
    config_schema = TelegramChannelConfig
    capabilities = ChannelCapabilities(
        streaming=True,
        media=True,
        reactions=True,
        threads=True,
        typing_indicator=True,
        message_edit=True,
        html_formatting=True,
        max_message_length=4096,
    )

    def __init__(self) -> None:
        self._gw: Optional[GatewayHandle] = None
        self._bots: Dict[str, Any] = {}  # bot_name → telegram.Bot
        self._chat_ids: Dict[str, Optional[str]] = {}  # bot_name → default chat_id
        self._polling_tasks: Dict[str, asyncio.Task] = {}
        self._telegram_config: Optional[TelegramChannelConfig] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, gateway: GatewayHandle) -> None:
        self._gw = gateway
        self._telegram_config = self._load_config(gateway)

        if not self._telegram_config.enabled:
            _logger.info("Telegram disabled or not configured")
            return

        try:
            from telegram import Bot
        except ImportError:
            _logger.warning("python-telegram-bot not installed, Telegram disabled")
            return

        # Build list of bots to init (multi-bot or legacy single-bot)
        bots_to_init = self._resolve_bots()

        for bot_name, token, effective_cfg in bots_to_init:
            try:
                bot = Bot(token=token)
                me = await bot.get_me()
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                    _logger.debug(f"Cleared webhook for bot '{bot_name}'")
                except Exception as wh_err:
                    _logger.warning(f"Could not clear webhook for bot '{bot_name}': {wh_err}")
                self._bots[bot_name] = bot
                allowed = getattr(effective_cfg, "allowed_users", None) or []
                self._chat_ids[bot_name] = str(allowed[0]) if allowed else None
                _logger.info(
                    f"Telegram bot '{bot_name}' initialized: @{me.username} "
                    f"(agent={getattr(effective_cfg, 'agent', None) or 'first'})"
                )
                await self._register_commands(bot)
            except Exception as e:
                _logger.error(f"Failed to initialize Telegram bot '{bot_name}': {e}")

        if self._bots:
            _logger.info(f"Telegram ready: {len(self._bots)} bot(s) — {list(self._bots)}")

        # Start polling tasks
        for bot_name, bot in self._bots.items():
            task = asyncio.create_task(
                self._poll_bot(bot_name, bot),
                name=f"telegram-poll-{bot_name}",
            )
            self._polling_tasks[bot_name] = task

    async def stop(self) -> None:
        for bot_name, task in list(self._polling_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._polling_tasks.clear()
        self._bots.clear()
        self._chat_ids.clear()

    # ── Outbound (gateway / fan-out calls these) ─────────────────────────

    async def send_message(
        self,
        target: MessageTarget,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        bot = self._resolve_bot(kwargs.get("bot_name"))
        chat_id = target.user_id or target.group_id
        if not bot or not chat_id:
            return None
        try:
            send_kwargs: Dict[str, Any] = {"chat_id": chat_id, "text": text}
            if parse_mode:
                send_kwargs["parse_mode"] = parse_mode
            msg = await bot.send_message(**send_kwargs)
            return str(msg.message_id)
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
        bot = self._resolve_bot(kwargs.get("bot_name"))
        chat_id = target.user_id or target.group_id
        if not bot or not chat_id:
            return
        try:
            edit_kwargs: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "text": text,
            }
            if parse_mode:
                edit_kwargs["parse_mode"] = parse_mode
            await bot.edit_message_text(**edit_kwargs)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                _logger.error(f"edit_message failed: {e}")

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
        **kwargs: Any,
    ) -> Optional[str]:
        bot = self._resolve_bot(kwargs.get("bot_name"))
        chat_id = target.user_id or target.group_id
        if not bot or not chat_id:
            return None
        mime = (media.mime_type or "").lower()
        try:
            if media.file_path:
                with open(media.file_path, "rb") as f:
                    if mime.startswith("image/"):
                        msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=media.caption)
                    elif mime.startswith("video/"):
                        msg = await bot.send_video(chat_id=chat_id, video=f, caption=media.caption)
                    else:
                        msg = await bot.send_document(chat_id=chat_id, document=f, caption=media.caption)
            elif media.url:
                if mime.startswith("image/"):
                    msg = await bot.send_photo(chat_id=chat_id, photo=media.url, caption=media.caption)
                elif mime.startswith("video/"):
                    msg = await bot.send_video(chat_id=chat_id, video=media.url, caption=media.caption)
                else:
                    msg = await bot.send_document(chat_id=chat_id, document=media.url, caption=media.caption)
            else:
                return None
            return str(msg.message_id)
        except Exception as e:
            _logger.error(f"send_media failed: {e}")
            return None

    async def react(
        self,
        target: MessageTarget,
        message_id: str,
        emoji: str,
    ) -> None:
        bot = self._resolve_bot()
        chat_id = target.user_id or target.group_id
        if bot and chat_id:
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    reaction=[{"type": "emoji", "emoji": emoji}],
                )
            except Exception as e:
                _logger.debug(f"react failed: {e}")

    async def send_typing(self, target: MessageTarget) -> None:
        bot = self._resolve_bot()
        chat_id = target.user_id or target.group_id
        if bot and chat_id:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

    # ── Polling ───────────────────────────────────────────────────────────

    async def _poll_bot(self, bot_name: str, bot: Any) -> None:
        """Long-poll one Telegram bot for incoming messages and dispatch them."""
        offset: Optional[int] = None
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=30)
                for update in updates:
                    if update.message and update.message.text:
                        asyncio.create_task(
                            self._handle_message(update.message, bot_name, bot)
                        )
                    if update.update_id is not None:
                        offset = update.update_id + 1
            except asyncio.CancelledError:
                _logger.debug(f"Polling cancelled for bot '{bot_name}'")
                return
            except Exception as e:
                _logger.error(f"Polling error for bot '{bot_name}': {e}")
                await asyncio.sleep(2)

    # ── Inbound message handler ──────────────────────────────────────────

    async def _handle_message(
        self,
        message: Any,
        bot_name: str = "_default",
        bot: Optional[Any] = None,
    ) -> None:
        """Route one incoming Telegram message to the agent and reply."""
        bot = bot or self._resolve_bot()
        if not bot or not self._gw:
            return

        user_id = str(message.from_user.id)
        chat_id = str(message.chat.id)
        text = message.text or ""
        tg_thread_id = (
            str(message.message_thread_id)
            if getattr(message, "message_thread_id", None)
            else None
        )

        _logger.info(
            f"Telegram message received: bot={bot_name} user={user_id} "
            f"chat={chat_id} msg_id={message.message_id} text={text[:60]!r}"
        )

        # Dedup: include bot_name so the same message_id across bots is not
        # incorrectly treated as a duplicate.
        if self._gw.is_duplicate(f"telegram/{bot_name}", str(message.message_id)):
            _logger.debug(f"Dropping duplicate Telegram message_id={message.message_id}")
            return

        # Access control (per-bot)
        uid_int = int(user_id)
        effective_cfg = self._effective_config(bot_name)
        allowed = getattr(effective_cfg, "allowed_users", []) or []
        denied = getattr(effective_cfg, "denied_users", []) or []
        if not self._gw.check_access(uid_int, allowed, denied):
            _logger.debug(f"Ignored message from unauthorized user {user_id} (bot={bot_name})")
            return

        sender_name = getattr(message.from_user, "first_name", None) or user_id

        # Resolve which agent handles this bot
        agent_id = self._agent_id_for_bot(bot_name)

        # Check for /focus thread binding override
        # (exposed via gateway handle's config, but thread bindings live in
        # gateway internals — pre-register the endpoint and let dispatch handle it)

        # Intercept slash commands before routing to the agent
        if text.strip().startswith("/"):
            reply = await self._gw.dispatch_command(
                channel="telegram",
                user_id=user_id,
                text=text.strip(),
                thread_id=tg_thread_id,
                agent_id=agent_id,
            )
            if reply is not None:
                try:
                    await bot.send_message(chat_id=chat_id, text=reply)
                except Exception as e:
                    _logger.error(f"Failed to send command reply: {e}")
                return

        # Pre-register endpoint so handle_message preserves bot_name
        self._gw.register_endpoint(agent_id, "telegram", {
            "sender_id": chat_id,
            "sender": sender_name,
            "bot_name": bot_name,
        })

        # Resolve per-bot streaming / typing indicator flags
        streaming = getattr(effective_cfg, "streaming", False)
        typing_indicator = getattr(effective_cfg, "typing_indicator", True)
        show_thinking = self._show_thinking_for_agent(agent_id)

        # Start typing indicator
        typing_task: Optional[asyncio.Task] = None
        if typing_indicator:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

            async def _keep_typing() -> None:
                while True:
                    await asyncio.sleep(4)
                    try:
                        await bot.send_chat_action(chat_id=chat_id, action="typing")
                    except Exception:
                        pass

            typing_task = asyncio.create_task(_keep_typing())

        try:
            if streaming:
                await self._handle_streaming(
                    chat_id=chat_id,
                    user_id=user_id,
                    sender_name=sender_name,
                    text=text,
                    message_id=str(message.message_id),
                    agent_id=agent_id,
                    bot_name=bot_name,
                    bot=bot,
                    show_thinking=show_thinking,
                )
            else:
                await self._handle_non_streaming(
                    chat_id=chat_id,
                    user_id=user_id,
                    sender_name=sender_name,
                    text=text,
                    message_id=str(message.message_id),
                    agent_id=agent_id,
                    bot_name=bot_name,
                    bot=bot,
                    show_thinking=show_thinking,
                )
        except asyncio.CancelledError:
            _logger.info(f"Telegram message cancelled for {user_id}")
        except Exception as e:
            _logger.error(f"Error handling Telegram message from {user_id} (bot={bot_name}): {e}")
            try:
                await bot.send_message(chat_id=chat_id, text=f"Sorry, I hit an error: {e}")
            except Exception:
                pass
        finally:
            if typing_task and not typing_task.done():
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    # ── Non-streaming path ────────────────────────────────────────────────

    async def _handle_non_streaming(
        self,
        chat_id: str,
        user_id: str,
        sender_name: str,
        text: str,
        message_id: str,
        agent_id: str,
        bot_name: str,
        bot: Any,
        show_thinking: bool,
    ) -> None:
        """Dispatch to gateway, send response as complete message(s)."""
        response = await self._gw.dispatch(
            channel="telegram",
            user_id=user_id,
            user_name=sender_name,
            text=text,
            message_id=message_id,
            agent_id=agent_id,
        )
        if response:
            await self._send_response(chat_id, bot, response, show_thinking)

    async def _send_response(
        self,
        chat_id: str,
        bot: Any,
        response: str,
        show_thinking: bool,
    ) -> None:
        """Format and send a complete response (non-streaming)."""
        if show_thinking:
            from pyclopse.agents.runner import format_thinking_for_telegram
            combined = format_thinking_for_telegram(response)
            if combined:
                for chunk in self._gw.split_message(combined):
                    await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                return
        from pyclopse.agents.runner import strip_thinking_tags
        clean = strip_thinking_tags(response)
        for chunk in self._gw.split_message(clean):
            await bot.send_message(chat_id=chat_id, text=chunk)

    # ── Streaming path ────────────────────────────────────────────────────

    async def _handle_streaming(
        self,
        chat_id: str,
        user_id: str,
        sender_name: str,
        text: str,
        message_id: str,
        agent_id: str,
        bot_name: str,
        bot: Any,
        show_thinking: bool,
    ) -> None:
        """Dispatch to gateway with streaming on_chunk, edit message in place."""
        from pyclopse.agents.runner import (
            strip_thinking_tags,
            format_thinking_for_telegram,
        )

        THROTTLE_S = 0.5
        stream_msg_id: Optional[int] = None
        last_edit_time: float = 0.0
        thinking_buffer: str = ""
        response_buffer: str = ""

        async def on_chunk(chunk_text: str, is_reasoning: bool) -> None:
            nonlocal stream_msg_id, last_edit_time, thinking_buffer, response_buffer

            if is_reasoning:
                thinking_buffer += chunk_text
            else:
                response_buffer += chunk_text

            # Build display text
            if show_thinking and thinking_buffer:
                tail = thinking_buffer[-600:] if len(thinking_buffer) > 600 else thinking_buffer
                safe_t = _html.escape(tail, quote=False)
                display = f"<blockquote expandable><i>💭 {safe_t}</i></blockquote>"
                response_part = _live_display(response_buffer)
                if response_part:
                    display += f"\n\n{_html.escape(response_part, quote=False)}"
                mid_parse_mode: Optional[str] = "HTML"
            else:
                display = _live_display(response_buffer)
                if not display:
                    return
                mid_parse_mode = None

            now = time.monotonic()
            if stream_msg_id is None:
                try:
                    msg = await bot.send_message(
                        chat_id=chat_id, text=display,
                        **({"parse_mode": mid_parse_mode} if mid_parse_mode else {}),
                    )
                    stream_msg_id = msg.message_id
                    last_edit_time = now
                except Exception as _se:
                    _logger.warning(f"Stream: initial send failed: {_se}")
            elif now - last_edit_time >= THROTTLE_S:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=stream_msg_id, text=display,
                        **({"parse_mode": mid_parse_mode} if mid_parse_mode else {}),
                    )
                    last_edit_time = now
                except Exception:
                    pass  # "message is not modified" etc.

        # Dispatch with streaming callback
        response = await self._gw.dispatch(
            channel="telegram",
            user_id=user_id,
            user_name=sender_name,
            text=text,
            message_id=message_id,
            agent_id=agent_id,
            on_chunk=on_chunk,
        )

        # Final edit with complete formatted response
        if response:
            clean_response = strip_thinking_tags(response)
            if show_thinking and thinking_buffer:
                safe_thinking = _html.escape(thinking_buffer.strip(), quote=False)
                safe_response = _html.escape(clean_response, quote=False)
                final_text = (
                    f"<blockquote expandable><i>💭 {safe_thinking}</i></blockquote>"
                    f"\n\n{safe_response}"
                )
                parse_mode: Optional[str] = "HTML"
            elif show_thinking:
                combined = format_thinking_for_telegram(response)
                if combined:
                    final_text = combined
                    parse_mode = "HTML"
                else:
                    final_text = clean_response
                    parse_mode = None
            else:
                final_text = clean_response
                parse_mode = None

            if not final_text:
                return

            send_kwargs: Dict[str, Any] = {"text": final_text}
            if parse_mode:
                send_kwargs["parse_mode"] = parse_mode

            try:
                if stream_msg_id is not None:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=stream_msg_id, **send_kwargs,
                    )
                else:
                    await bot.send_message(chat_id=chat_id, **send_kwargs)
            except Exception as fe:
                if "message is not modified" not in str(fe).lower():
                    _logger.error(f"Stream: final edit failed: {fe}")
                    try:
                        await bot.send_message(chat_id=chat_id, **send_kwargs)
                    except Exception:
                        pass

    # ── Bot resolution helpers ────────────────────────────────────────────

    def _resolve_bots(self) -> List[Tuple[str, str, Any]]:
        """Build (bot_name, token, effective_config) list from TelegramConfig."""
        cfg = self._telegram_config
        if not cfg:
            return []

        result: List[Tuple[str, str, Any]] = []
        if cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.bot_token:
                    result.append((bot_name, effective.bot_token, effective))
                else:
                    _logger.warning(f"Telegram bot '{bot_name}' has no botToken, skipping")
        elif cfg.bot_token:
            result.append(("_default", cfg.bot_token, cfg))

        return result

    def _resolve_bot(self, bot_name: Optional[str] = None) -> Optional[Any]:
        """Get a Bot instance by name, or first available."""
        if bot_name and bot_name in self._bots:
            return self._bots[bot_name]
        return next(iter(self._bots.values()), None)

    def _effective_config(self, bot_name: str) -> Any:
        """Return effective per-bot config (falls back to parent)."""
        cfg = self._telegram_config
        if not cfg:
            return None
        if cfg.bots and bot_name in cfg.bots:
            return cfg.effective_config_for_bot(bot_name)
        return cfg

    def _agent_id_for_bot(self, bot_name: str) -> str:
        """Resolve which agent handles this bot's messages."""
        cfg = self._telegram_config
        if cfg and cfg.bots and bot_name in cfg.bots:
            effective = cfg.effective_config_for_bot(bot_name)
            if effective.agent:
                return self._gw.resolve_agent_id(effective.agent)
        return self._gw.resolve_agent_id()

    def bot_for_agent(self, agent_id: str) -> Tuple[Optional[Any], Optional[str]]:
        """Return (bot_instance, bot_name) for the bot configured for *agent_id*."""
        cfg = self._telegram_config
        if cfg and cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.agent == agent_id and bot_name in self._bots:
                    return self._bots[bot_name], bot_name
        if self._bots:
            bot_name = next(iter(self._bots))
            return self._bots[bot_name], bot_name
        return None, None

    def _show_thinking_for_agent(self, agent_id: str) -> bool:
        """Check if agent has show_thinking enabled."""
        try:
            agent_cfg = self._gw.get_agent_config(agent_id)
            if agent_cfg:
                return getattr(agent_cfg, "show_thinking", False)
            return False
        except Exception:
            return False

    async def _register_commands(self, bot: Any) -> None:
        """Register slash commands with Telegram command picker."""
        try:
            from telegram import BotCommand
            # Access command list via gateway handle
            gw_impl = self._gw
            if hasattr(gw_impl, "_gw"):
                # _GatewayHandleImpl — access the underlying gateway
                registry = getattr(gw_impl._gw, "_command_registry", None)
                if registry:
                    commands = [
                        BotCommand(cmd, desc)
                        for cmd, desc in registry.commands_for_telegram()
                    ]
                    await bot.set_my_commands(commands)
                    _logger.info(f"Registered {len(commands)} Telegram commands")
        except Exception as e:
            _logger.warning(f"Failed to register Telegram commands: {e}")


# ── Module-level helpers ──────────────────────────────────────────────────

def _live_display(buf: str) -> str:
    """Return text safe to show mid-stream.

    Strips complete <think>…</think> blocks, then hides everything from any
    still-open <think> tag to the end of the buffer.
    """
    from pyclopse.agents.runner import strip_thinking_tags
    stripped = strip_thinking_tags(buf)
    m = _OPEN_THINK.search(stripped)
    if m:
        return stripped[: m.start()].strip()
    return stripped
