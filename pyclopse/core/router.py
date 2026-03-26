"""Message routing for pyclopse."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pyclopse.utils.time import now
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from pyclopse.config.schema import Config


class RouteTarget(str, Enum):
    """Where to route a message.

    Attributes:
        AGENT: Route to an agent for processing.
        SESSION: Route to an existing session.
        BROADCAST: Broadcast to all registered channel handlers.
    """

    AGENT = "agent"
    SESSION = "session"
    BROADCAST = "broadcast"


@dataclass
class IncomingMessage:
    """Incoming message from a channel.

    Attributes:
        id (str): Unique message identifier (used for deduplication).
        channel (str): Source channel name (e.g. "telegram", "slack", "tui").
        sender (str): Human-readable sender name or username.
        sender_id (str): Stable sender identifier (user ID).
        content (str): Raw message text content.
        timestamp (datetime): When the message arrived. Defaults to current time.
        metadata (Dict[str, Any]): Channel-specific extra fields (e.g. thread_ts).
        reply_to (Optional[str]): Message ID this is a reply to, if any.
        thread_id (Optional[str]): Telegram topic ID or Slack thread timestamp.
    """

    id: str
    channel: str
    sender: str
    sender_id: str
    content: str
    timestamp: datetime = field(default_factory=now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reply_to: Optional[str] = None
    thread_id: Optional[str] = None


@dataclass
class OutgoingMessage:
    """Outgoing message to a channel.

    Attributes:
        content (str): Text content to send.
        target (str): Destination user or chat identifier.
        channel (str): Destination channel name.
        reply_to (Optional[str]): Message ID to reply to, if applicable.
        metadata (Dict[str, Any]): Channel-specific delivery metadata.
    """

    content: str
    target: str
    channel: str
    reply_to: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# Route handler type
RouteHandler = Callable[[IncomingMessage], Awaitable[Optional[OutgoingMessage]]]


class RouteRule:
    """Rule for routing messages to a handler.

    Attributes:
        name (str): Human-readable rule identifier.
        channel (Optional[str]): If set, only matches messages from this channel.
        sender_pattern (Optional[str]): Regex applied to message.sender; must match if set.
        content_pattern (Optional[str]): Regex applied to message.content; must match if set.
        priority (int): Higher-priority rules are evaluated first. Defaults to 0.
    """

    def __init__(
        self,
        name: str,
        channel: Optional[str] = None,
        sender_pattern: Optional[str] = None,
        content_pattern: Optional[str] = None,
        priority: int = 0,
    ):
        """Initialize a RouteRule.

        Args:
            name (str): Human-readable rule identifier.
            channel (Optional[str]): If set, only matches messages on this channel.
            sender_pattern (Optional[str]): Regex applied to message.sender.
            content_pattern (Optional[str]): Regex applied to message.content.
            priority (int): Higher values are evaluated first. Defaults to 0.
        """
        self.name = name
        self.channel = channel
        self.sender_pattern = sender_pattern
        self.content_pattern = content_pattern
        self.priority = priority

    def matches(self, message: IncomingMessage) -> bool:
        """Check if this rule matches the message.

        All configured predicates (channel, sender_pattern, content_pattern)
        must match simultaneously for the rule to apply.

        Args:
            message (IncomingMessage): The incoming message to evaluate.

        Returns:
            bool: True if all predicates match; False otherwise.
        """
        import re
        
        # Check channel
        if self.channel and message.channel != self.channel:
            return False
        
        # Check sender pattern
        if self.sender_pattern:
            if not re.match(self.sender_pattern, message.sender):
                return False
        
        # Check content pattern
        if self.content_pattern:
            if not re.search(self.content_pattern, message.content):
                return False
        
        return True


class MessageRouter:
    """Routes incoming messages to appropriate handlers.

    Dispatches messages using a three-tier priority chain:
    1. Per-channel handlers (exact channel name match).
    2. Rule-based handlers (evaluated in descending priority order).
    3. A global default handler (catch-all).

    Attributes:
        config (Config): The loaded pyclopse configuration.
    """

    def __init__(self, config: Config):
        """Initialize the MessageRouter.

        Args:
            config (Config): The loaded pyclopse configuration.
        """
        self.config = config
        self._handlers: Dict[str, RouteHandler] = {}
        self._rules: List[RouteRule] = []
        self._default_handler: Optional[RouteHandler] = None
        self._channel_handlers: Dict[str, RouteHandler] = {}
        self._logger = logging.getLogger("pyclopse.router")

    def register_handler(self, name: str, handler: RouteHandler) -> None:
        """Register a named message handler.

        Args:
            name (str): Unique handler name for later lookup or removal.
            handler (RouteHandler): Async callable that processes an IncomingMessage.
        """
        self._handlers[name] = handler
        self._logger.debug(f"Registered handler: {name}")

    def register_channel_handler(
        self,
        channel: str,
        handler: RouteHandler,
    ) -> None:
        """Register a handler that receives all messages from a specific channel.

        Args:
            channel (str): Channel name (e.g. "telegram", "slack").
            handler (RouteHandler): Async callable invoked for every message on
                this channel.
        """
        self._channel_handlers[channel] = handler
        self._logger.debug(f"Registered channel handler: {channel}")

    def set_default_handler(self, handler: RouteHandler) -> None:
        """Set the fallback handler used when no channel or rule matches.

        Args:
            handler (RouteHandler): Async callable invoked for unmatched messages.
        """
        self._default_handler = handler

    def add_rule(self, rule: RouteRule, handler: RouteHandler) -> None:
        """Add a routing rule and its associated handler.

        Rules are kept sorted by descending priority so that higher-priority
        rules are evaluated first during routing.

        Args:
            rule (RouteRule): The matching rule to add.
            handler (RouteHandler): Handler invoked when the rule matches.
        """
        self._rules.append((rule, handler))
        # Sort by priority (higher first)
        self._rules.sort(key=lambda x: x[0].priority, reverse=True)

    def remove_handler(self, name: str) -> bool:
        """Remove a named handler by name.

        Args:
            name (str): The handler name passed to register_handler().

        Returns:
            bool: True if the handler existed and was removed; False otherwise.
        """
        return self._handlers.pop(name, None) is not None

    async def route(self, message: IncomingMessage) -> Optional[OutgoingMessage]:
        """Route a message to the appropriate handler.

        Dispatches using the three-tier chain: channel handler → rule match
        → default handler.  Logs a warning when no handler is found.

        Args:
            message (IncomingMessage): The message to route.

        Returns:
            Optional[OutgoingMessage]: The handler's response, or None if no
                handler returned a result.
        """
        self._logger.debug(f"Routing message from {message.sender} on {message.channel}")

        # First, check channel-specific handlers
        if message.channel in self._channel_handlers:
            handler = self._channel_handlers[message.channel]
            return await handler(message)

        # Then check rules
        for rule, handler in self._rules:
            if rule.matches(message):
                self._logger.debug(f"Message matched rule: {rule.name}")
                return await handler(message)

        # Finally, use default handler
        if self._default_handler:
            return await self._default_handler(message)

        self._logger.warning(f"No handler for message from {message.sender}")
        return None

    async def broadcast(
        self,
        content: str,
        channels: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """Broadcast a message to multiple channels.

        Creates a synthetic system IncomingMessage and passes it to each
        channel's registered handler.  Errors per channel are caught and
        recorded without aborting other deliveries.

        Args:
            content (str): Text content to broadcast.
            channels (Optional[List[str]]): Channel names to target.  Defaults
                to all channels that have a registered handler.

        Returns:
            Dict[str, bool]: Mapping of channel name to delivery success flag.
        """
        results = {}
        target_channels = channels or list(self._channel_handlers.keys())

        for channel in target_channels:
            if channel in self._channel_handlers:
                # Create a broadcast message
                msg = IncomingMessage(
                    id="broadcast",
                    channel=channel,
                    sender="system",
                    sender_id="system",
                    content=content,
                )
                try:
                    result = await self._channel_handlers[channel](msg)
                    results[channel] = result is not None
                except Exception as e:
                    self._logger.error(f"Broadcast to {channel} failed: {e}")
                    results[channel] = False
            else:
                results[channel] = False

        return results

    def get_registered_handlers(self) -> List[str]:
        """Get list of registered handler names.

        Returns:
            List[str]: Names of all handlers registered via register_handler().
        """
        return list(self._handlers.keys())

    def get_channel_handlers(self) -> List[str]:
        """Get list of channels that have registered handlers.

        Returns:
            List[str]: Channel names with active handlers.
        """
        return list(self._channel_handlers.keys())


class RouterMixin:
    """Mixin to add routing capability to a class.

    Provides a lazily-injected ``_router`` attribute and a ``send_message``
    stub that subclasses are expected to implement.
    """

    def __init__(self):
        """Initialize the mixin with no router attached."""
        self._router: Optional[MessageRouter] = None

    @property
    def router(self) -> MessageRouter:
        """Return the attached MessageRouter.

        Raises:
            RuntimeError: If set_router() has not been called yet.

        Returns:
            MessageRouter: The active router instance.
        """
        if self._router is None:
            raise RuntimeError("Router not initialized")
        return self._router

    def set_router(self, router: MessageRouter) -> None:
        """Attach a MessageRouter to this mixin.

        Args:
            router (MessageRouter): The router instance to attach.
        """
        self._router = router

    async def send_message(
        self,
        content: str,
        target: str,
        channel: str,
    ) -> None:
        """Send a message through the router.

        Subclasses must override this method with channel-specific delivery logic.

        Args:
            content (str): Text content to send.
            target (str): Destination identifier (user ID, chat ID, etc.).
            channel (str): Target channel name.

        Raises:
            NotImplementedError: Always — subclasses must implement this method.
        """
        message = OutgoingMessage(
            content=content,
            target=target,
            channel=channel,
        )
        # This would be implemented by subclasses
        raise NotImplementedError
