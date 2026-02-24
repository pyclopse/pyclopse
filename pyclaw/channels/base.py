"""Base channel adapter abstract class."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, List, Callable, Awaitable
import asyncio


@dataclass
class Message:
    """Represents an incoming message from a channel."""
    id: str
    channel: str
    sender: str
    sender_name: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reply_to: Optional[str] = None  # Message ID to reply to


@dataclass
class MessageTarget:
    """Target for sending a message."""
    channel: str
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None  # For replies


@dataclass
class MediaAttachment:
    """Media attachment for messages."""
    url: Optional[str] = None
    file_path: Optional[str] = None
    mime_type: Optional[str] = None
    caption: Optional[str] = None


# Type alias for message handler
MessageHandler = Callable[[Message], Awaitable[None]]


class ChannelAdapter(ABC):
    """
    Abstract base class for channel adapters.
    
    Each adapter handles communication with a specific messaging platform
    (Telegram, Discord, Slack, etc.).
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: Channel-specific configuration dict
        """
        self.config = config
        self._handler: Optional[MessageHandler] = None
        self._running = False
        self._listener_task: Optional[asyncio.Task] = None
    
    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return the channel name (e.g., 'telegram', 'discord')."""
        pass
    
    @property
    def is_connected(self) -> bool:
        """Check if the channel is connected."""
        return self._running
    
    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the channel."""
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the channel."""
        pass
    
    @abstractmethod
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """
        Send a message to the channel.
        
        Args:
            target: MessageTarget describing where to send
            content: Message text content
            reply_to: Optional message ID to reply to
            
        Returns:
            Message ID of the sent message
        """
        pass
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """
        Send media (image, video, etc.) to the channel.
        
        Args:
            target: MessageTarget describing where to send
            media: MediaAttachment with media info
            
        Returns:
            Message ID of the sent message
        """
        raise NotImplementedError(
            f"Channel {self.channel_name} does not support media"
        )
    
    @abstractmethod
    async def react(self, message_id: str, emoji: str) -> None:
        """
        Add reaction to a message.
        
        Args:
            message_id: ID of the message to react to
            emoji: Emoji to add (platform-specific format)
        """
        pass
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """
        Handle incoming webhook request.
        
        Args:
            payload: Webhook payload from the platform
            
        Returns:
            Parsed Message if valid, None otherwise
        """
        # Default implementation - override for webhook support
        return None
    
    def set_handler(self, handler: MessageHandler) -> None:
        """Set the message handler callback."""
        self._handler = handler
    
    async def start_listening(self) -> None:
        """Start listening for incoming messages."""
        if self._running:
            return
        
        self._running = True
        self._listener_task = asyncio.create_task(
            self._listen(),
            name=f"channel-{self.channel_name}-listen"
        )
    
    async def stop_listening(self) -> None:
        """Stop listening for incoming messages."""
        self._running = False
        
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
    
    async def _listen(self) -> None:
        """
        Internal listener loop. Override for polling-based adapters.
        """
        # Default implementation does nothing
        # Override for platforms that need polling
        while self._running:
            await asyncio.sleep(1)
    
    async def _dispatch(self, message: Message) -> None:
        """Dispatch a received message to the handler."""
        if self._handler:
            try:
                await self._handler(message)
            except Exception as e:
                # Log but don't crash
                import logging
                logging.getLogger(f"pyclaw.channels.{self.channel_name}").error(
                    f"Error handling message: {e}"
                )
