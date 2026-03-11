"""Message routing for pyclaw."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pyclaw.utils.time import now
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from pyclaw.config.schema import Config


class RouteTarget(str, Enum):
    """Where to route a message."""
    AGENT = "agent"
    SESSION = "session"
    BROADCAST = "broadcast"


@dataclass
class IncomingMessage:
    """Incoming message from a channel."""
    id: str
    channel: str
    sender: str
    sender_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reply_to: Optional[str] = None
    thread_id: Optional[str] = None


@dataclass
class OutgoingMessage:
    """Outgoing message to a channel."""
    content: str
    target: str
    channel: str
    reply_to: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# Route handler type
RouteHandler = Callable[[IncomingMessage], Awaitable[Optional[OutgoingMessage]]]


class RouteRule:
    """Rule for routing messages."""
    
    def __init__(
        self,
        name: str,
        channel: Optional[str] = None,
        sender_pattern: Optional[str] = None,
        content_pattern: Optional[str] = None,
        priority: int = 0,
    ):
        self.name = name
        self.channel = channel
        self.sender_pattern = sender_pattern
        self.content_pattern = content_pattern
        self.priority = priority
    
    def matches(self, message: IncomingMessage) -> bool:
        """Check if this rule matches the message."""
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
    """Routes incoming messages to appropriate handlers."""
    
    def __init__(self, config: Config):
        self.config = config
        self._handlers: Dict[str, RouteHandler] = {}
        self._rules: List[RouteRule] = []
        self._default_handler: Optional[RouteHandler] = None
        self._channel_handlers: Dict[str, RouteHandler] = {}
        self._logger = logging.getLogger("pyclaw.router")
    
    def register_handler(self, name: str, handler: RouteHandler) -> None:
        """Register a message handler."""
        self._handlers[name] = handler
        self._logger.debug(f"Registered handler: {name}")
    
    def register_channel_handler(
        self,
        channel: str,
        handler: RouteHandler,
    ) -> None:
        """Register a handler for a specific channel."""
        self._channel_handlers[channel] = handler
        self._logger.debug(f"Registered channel handler: {channel}")
    
    def set_default_handler(self, handler: RouteHandler) -> None:
        """Set the default handler for unmatched messages."""
        self._default_handler = handler
    
    def add_rule(self, rule: RouteRule, handler: RouteHandler) -> None:
        """Add a routing rule."""
        self._rules.append((rule, handler))
        # Sort by priority (higher first)
        self._rules.sort(key=lambda x: x[0].priority, reverse=True)
    
    def remove_handler(self, name: str) -> bool:
        """Remove a handler by name."""
        return self._handlers.pop(name, None) is not None
    
    async def route(self, message: IncomingMessage) -> Optional[OutgoingMessage]:
        """Route a message to the appropriate handler."""
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
        """Broadcast a message to multiple channels."""
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
        """Get list of registered handler names."""
        return list(self._handlers.keys())
    
    def get_channel_handlers(self) -> List[str]:
        """Get list of channels with handlers."""
        return list(self._channel_handlers.keys())


class RouterMixin:
    """Mixin to add routing capability to a class."""
    
    def __init__(self):
        self._router: Optional[MessageRouter] = None
    
    @property
    def router(self) -> MessageRouter:
        if self._router is None:
            raise RuntimeError("Router not initialized")
        return self._router
    
    def set_router(self, router: MessageRouter) -> None:
        self._router = router
    
    async def send_message(
        self,
        content: str,
        target: str,
        channel: str,
    ) -> None:
        """Send a message through the router."""
        message = OutgoingMessage(
            content=content,
            target=target,
            channel=channel,
        )
        # This would be implemented by subclasses
        raise NotImplementedError
