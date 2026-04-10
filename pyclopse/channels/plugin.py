"""
ChannelPlugin — unified base class for gateway-integrated channel plugins.

A channel plugin connects a messaging platform to the pyclopse gateway.
It has four responsibilities:

1. **Lifecycle** — ``start(gateway)`` and ``stop()`` called by the gateway.
2. **Inbound** — receive messages from the platform and call
   ``gateway.dispatch(...)`` to deliver them to the agent.
3. **Outbound** — implement ``send_message(target, text)`` so the gateway
   (fan-out, delivery) can send replies back to the platform.
4. **Capabilities** — declare what the channel supports (streaming, media,
   reactions, etc.) via :class:`ChannelCapabilities`.

Minimal example::

    from pyclopse.channels.plugin import ChannelPlugin, GatewayHandle
    from pyclopse.channels.base import MessageTarget

    class MyPlugin(ChannelPlugin):
        name = "myplugin"

        async def start(self, gateway: GatewayHandle) -> None:
            self._gw = gateway
            # connect to platform, start polling / webhook, etc.

        async def stop(self) -> None:
            pass  # tear down connections

        async def send_message(self, target: MessageTarget, text: str,
                               parse_mode=None, **kwargs) -> None:
            # send text to the platform user identified by target.user_id
            ...

Plugin registration
-------------------
Plugins are discovered in two ways (tried in order):

1. **Entry points** — any installed package that declares::

       [project.entry-points."pyclopse.channels"]
       myplugin = "mypackage.plugin:MyPlugin"

2. **Explicit config** — ``plugins.channels`` list in ``pyclopse.yaml``::

       plugins:
         channels:
           - mypackage.plugin:MyPlugin
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from pydantic import AliasChoices  # noqa: F401 — re-exported for plugin authors

from pyclopse.reflect import reflect_system

# Re-export data classes from base so plugin authors get everything from one import.
from pyclopse.channels.base import MediaAttachment, Message, MessageTarget  # noqa: F401

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Channel config (base for plugin-declared schemas)
# ---------------------------------------------------------------------------

class ChannelConfig(BaseModel):
    """Base config model shared by all channels.

    Plugins extend this with platform-specific fields by subclassing and
    setting ``config_schema`` on their :class:`ChannelPlugin`::

        class MyChannelConfig(ChannelConfig):
            api_key: str = Field(default=None, validation_alias="apiKey")

        class MyPlugin(ChannelPlugin):
            config_schema = MyChannelConfig

    The base class uses ``extra="allow"`` so any fields not declared by a
    subclass are silently accepted (useful when the gateway parses raw YAML
    before a plugin validates it).  Plugin subclasses that want strict
    validation can override with ``model_config = ConfigDict(extra="forbid")``.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    allowed_users: list = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: list = Field(default_factory=list, validation_alias="deniedUsers")
    typing_indicator: bool = Field(
        default=True,
        validation_alias=AliasChoices("typing_indicator", "typingIndicator"),
    )
    streaming: bool = False


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

@dataclass
class ChannelCapabilities:
    """Declares what a channel plugin supports.

    The gateway inspects these flags to decide which outbound methods to call
    and how to format content for this channel.
    """

    streaming: bool = False
    """Channel can display streaming edits (send initial, then edit in-place)."""

    media: bool = False
    """Channel can send images, video, and file attachments."""

    reactions: bool = False
    """Channel can add emoji reactions to messages."""

    threads: bool = False
    """Channel supports threaded replies."""

    typing_indicator: bool = False
    """Channel can show a "typing..." status."""

    message_edit: bool = False
    """Channel can edit previously sent messages."""

    html_formatting: bool = False
    """Channel supports HTML markup in messages."""

    max_message_length: int = 4096
    """Platform's maximum message length in characters."""


# ---------------------------------------------------------------------------
# Gateway handle
# ---------------------------------------------------------------------------

@reflect_system("channels")
class GatewayHandle:
    """Gateway interface exposed to channel plugins.

    Plugins use this handle for all interactions with the gateway: dispatching
    inbound messages, checking access control, resolving agents, and more.

    At runtime the gateway creates a concrete ``_GatewayHandleImpl`` subclass.
    All methods on this base raise ``NotImplementedError``.
    """

    # -- Core dispatch --------------------------------------------------------

    async def dispatch(
        self,
        channel: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        on_chunk: Optional[Callable[[str, bool], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """Deliver an inbound message to the gateway and return the agent reply.

        Args:
            channel: Channel identifier, e.g. ``"telegram"``.
            user_id: Platform user ID (string).
            user_name: Human-readable display name.
            text: Message content.
            message_id: Optional platform message ID for deduplication.
            agent_id: Route to a specific agent; ``None`` → first agent.
            on_chunk: Async callback ``(chunk_text, is_reasoning)`` fired for
                each streaming chunk.  The gateway wraps this inside its own
                event-bus publisher so both the event bus and the caller
                receive chunks.

        Returns:
            Agent reply text, or ``None`` if no response was generated.
        """
        raise NotImplementedError

    async def dispatch_command(
        self,
        channel: str,
        user_id: str,
        text: str,
        thread_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Try to dispatch a ``/slash`` command.

        Returns:
            Command reply text, or ``None`` if *text* is not a recognised
            command (the plugin should then route to ``dispatch`` instead).
        """
        raise NotImplementedError

    # -- Helpers --------------------------------------------------------------

    def is_duplicate(self, channel: str, message_id: str) -> bool:
        """Check whether *message_id* was already seen on *channel* (60 s TTL)."""
        raise NotImplementedError

    def resolve_agent_id(self, hint: Optional[str] = None) -> str:
        """Resolve *hint* to a valid agent ID; falls back to first agent."""
        raise NotImplementedError

    def check_access(
        self,
        user_id: int,
        allowed_users: List[int],
        denied_users: List[int],
    ) -> bool:
        """Check global + channel access control.

        Returns ``True`` if the user is allowed to send messages.
        """
        raise NotImplementedError

    def register_endpoint(
        self,
        agent_id: str,
        channel: str,
        endpoint: Dict[str, Any],
    ) -> None:
        """Pre-register an endpoint dict (e.g. ``bot_name``) before dispatch.

        ``handle_message`` uses ``setdefault`` on the channel key, so fields
        set here (like ``bot_name``) are preserved when dispatch later writes
        ``sender_id`` and ``sender``.
        """
        raise NotImplementedError

    def get_agent_config(self, agent_id: str) -> Optional[Any]:
        """Return the live ``AgentConfig`` for *agent_id*, or ``None``."""
        raise NotImplementedError

    def split_message(self, text: str, limit: int = 4096) -> List[str]:
        """Split *text* into chunks of at most *limit* characters."""
        raise NotImplementedError

    @property
    def config(self) -> Any:
        """Read-only access to the gateway config object."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Channel plugin ABC
# ---------------------------------------------------------------------------

@reflect_system("channels")
class ChannelPlugin(ABC):
    """Abstract base class for gateway-integrated channel plugins.

    Subclass this to add a new messaging channel.  At minimum, override
    :meth:`start`, :meth:`stop`, and :meth:`send_message`.

    Attributes:
        name: Channel identifier used for registration.
        capabilities: Declares what the channel supports.
    """

    name: str = ""
    capabilities: ChannelCapabilities = ChannelCapabilities()
    config_schema: type[ChannelConfig] = ChannelConfig
    """Pydantic model for this channel's config.  Override in subclasses."""

    # -- Config helper --------------------------------------------------------

    def _load_config(self, gateway: "GatewayHandle") -> "ChannelConfig":
        """Load and validate this plugin's config from the gateway.

        Reads ``gateway.config.channels.{self.name}`` (raw dict or Pydantic
        model) and validates it against :attr:`config_schema`.  Returns a
        default (disabled) config if the channel is not configured.
        """
        channels = gateway.config.channels
        if channels is None:
            return self.config_schema()
        # Try attribute access first (Pydantic model), then dict-style
        raw = getattr(channels, self.name, None)
        if raw is None and isinstance(channels, dict):
            raw = channels.get(self.name)
        if raw is None:
            return self.config_schema()
        # Normalize to dict for model_validate
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(by_alias=True)
        elif not isinstance(raw, dict):
            raw = dict(raw)
        return self.config_schema.model_validate(raw)

    # -- Lifecycle ------------------------------------------------------------

    @abstractmethod
    async def start(self, gateway: GatewayHandle) -> None:
        """Start the channel.

        Called by the gateway during ``Gateway.initialize()``.  Implementors
        must establish connections, start polling loops, register webhooks,
        and store the *gateway* handle for later use.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel.

        Called during ``Gateway.stop()``.  Cancel polling tasks, close
        connections, and release resources.
        """

    # -- Outbound (gateway / fan-out calls these) -----------------------------

    @abstractmethod
    async def send_message(
        self,
        target: "MessageTarget",
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """Send a text message to the platform.

        Args:
            target: Destination (user, group, or thread).
            text: Message content.
            parse_mode: Optional formatting hint (e.g. ``"HTML"``).
            **kwargs: Platform-specific extras (e.g. ``bot_name``).

        Returns:
            Platform-assigned message ID, or ``None``.
        """

    async def edit_message(
        self,
        target: "MessageTarget",
        message_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Edit a previously sent message (optional).

        Only called if ``capabilities.message_edit`` is ``True``.
        """
        raise NotImplementedError(f"{self.name} does not support message editing")

    async def send_media(
        self,
        target: "MessageTarget",
        media: "MediaAttachment",
        **kwargs: Any,
    ) -> Optional[str]:
        """Send a media attachment (optional).

        Only called if ``capabilities.media`` is ``True``.

        Returns:
            Platform-assigned message ID, or ``None``.
        """
        raise NotImplementedError(f"{self.name} does not support media")

    async def react(
        self,
        target: "MessageTarget",
        message_id: str,
        emoji: str,
    ) -> None:
        """Add an emoji reaction to a message (optional).

        Only called if ``capabilities.reactions`` is ``True``.
        """
        raise NotImplementedError(f"{self.name} does not support reactions")

    async def send_typing(self, target: "MessageTarget") -> None:
        """Show a typing indicator (optional).

        Only called if ``capabilities.typing_indicator`` is ``True``.
        Default implementation is a no-op.
        """

    # -- Webhook (for channels that receive inbound via HTTP callbacks) -------

    async def handle_webhook(
        self,
        request_body: bytes,
        headers: Dict[str, str],
        query_params: Dict[str, str],
    ) -> Optional[Any]:
        """Handle an incoming webhook request (optional).

        Called by the generic ``/webhook/{channel}`` REST route for channels
        that receive messages via HTTP callbacks rather than persistent
        connections.

        Args:
            request_body: Raw request body bytes.
            headers: HTTP request headers (lowercase keys).
            query_params: URL query parameters.

        Returns:
            Response to send back.  A ``str`` is sent as plain text, a
            ``dict`` as JSON, or ``None`` for a bare 200 OK.
        """
        raise NotImplementedError(f"{self.name} does not support webhooks")

    # -- Backward compatibility -----------------------------------------------

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Legacy send method — wraps :meth:`send_message`.

        Existing plugins that only override ``send()`` will continue to work.
        New plugins should override ``send_message()`` instead.
        """
        target = MessageTarget(channel=self.name, user_id=user_id)
        await self.send_message(target, text, **kwargs)
