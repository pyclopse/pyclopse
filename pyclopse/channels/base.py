"""Base channel adapter abstract class."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, List, Callable, Awaitable
import asyncio


@dataclass
class Message:
    """Represents an incoming message from a channel.

    Attributes:
        id (str): Platform-specific unique message identifier.
        channel (str): Channel name the message arrived on, e.g. ``"telegram"``.
        sender (str): Platform user ID of the message sender.
        sender_name (str): Human-readable display name of the sender.
        content (str): Text body of the message.
        timestamp (datetime): When the message was received. Defaults to now.
        metadata (Dict[str, Any]): Platform-specific extra fields such as
            ``chat_id``, ``thread_ts``, or ``guild_id``.
        reply_to (Optional[str]): Message ID this message is replying to,
            or ``None`` if it is a top-level message.
    """

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
    """Target for sending a message.

    Attributes:
        channel (str): Destination channel name, e.g. ``"slack"``.
        user_id (Optional[str]): Platform user ID for direct messages.
        group_id (Optional[str]): Platform group/room/space ID for group sends.
        thread_id (Optional[str]): Thread identifier for threaded replies.
        message_id (Optional[str]): Message ID to use when constructing
            a reply reference.
    """

    channel: str
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None  # For replies


@dataclass
class MediaAttachment:
    """Media attachment for messages.

    Attributes:
        url (Optional[str]): Public URL of the media file.
        file_path (Optional[str]): Local filesystem path to the media file.
        mime_type (Optional[str]): MIME type of the media, e.g.
            ``"image/jpeg"`` or ``"video/mp4"``.
        caption (Optional[str]): Optional caption text to accompany the media.
    """

    url: Optional[str] = None
    file_path: Optional[str] = None
    mime_type: Optional[str] = None
    caption: Optional[str] = None


# Type alias for message handler
MessageHandler = Callable[[Message], Awaitable[None]]


class ChannelAdapter(ABC):
    """Abstract base class for channel adapters.

    Each adapter handles communication with a specific messaging platform
    (Telegram, Discord, Slack, etc.). Subclasses implement platform-specific
    connection, sending, and webhook/polling logic.

    Attributes:
        config (Dict[str, Any]): Channel-specific configuration dictionary
            passed at construction time.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the channel adapter with platform configuration.

        Args:
            config (Dict[str, Any]): Channel-specific configuration dictionary.
                Keys vary by platform (e.g. ``bot_token``, ``signing_secret``).
        """
        self.config = config
        self._handler: Optional[MessageHandler] = None
        self._running = False
        self._listener_task: Optional[asyncio.Task] = None

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return the canonical channel name for this adapter.

        Implementors must return a short lowercase identifier such as
        ``"telegram"``, ``"discord"``, or ``"slack"``.

        Returns:
            str: The channel name.
        """
        pass

    @property
    def is_connected(self) -> bool:
        """Check whether the channel listener is currently running.

        Returns:
            bool: ``True`` if the adapter has been started and is running,
                ``False`` otherwise.
        """
        return self._running

    @abstractmethod
    async def connect(self) -> None:
        """Establish a connection to the messaging platform.

        Implementors should authenticate, verify credentials, and prepare
        any platform clients or sessions needed for sending and receiving
        messages.

        Raises:
            RuntimeError: If the required library is not installed or
                authentication fails.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection to the messaging platform.

        Implementors should close HTTP sessions, stop background tasks,
        and release any resources held by the platform client.
        """
        pass

    @abstractmethod
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message to the channel.

        Args:
            target (MessageTarget): Destination describing where to send the
                message (user, group, or thread).
            content (str): Text content of the message.
            reply_to (Optional[str]): Platform message ID to reply to.
                Defaults to None.

        Returns:
            str: Platform-assigned message ID of the sent message.

        Raises:
            RuntimeError: If the adapter is not connected.
            ValueError: If no valid target is specified in ``target``.
        """
        pass

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send a media attachment (image, video, document, etc.) to the channel.

        The base implementation raises ``NotImplementedError``. Override in
        subclasses for platforms that support media uploads.

        Args:
            target (MessageTarget): Destination describing where to send the
                media.
            media (MediaAttachment): Media content, either by URL or local
                file path.

        Returns:
            str: Platform-assigned message ID of the sent message.

        Raises:
            NotImplementedError: If the channel does not support media uploads.
        """
        raise NotImplementedError(
            f"Channel {self.channel_name} does not support media"
        )

    @abstractmethod
    async def react(self, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to an existing message.

        Args:
            message_id (str): Platform-specific ID of the message to react to.
            emoji (str): Emoji to add. Format is platform-specific (e.g. a
                Unicode character, a Slack ``:name:`` string, or a Discord
                custom emote string).
        """
        pass

    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Parse and handle an incoming webhook request from the platform.

        The default implementation returns ``None``. Override in subclasses
        that receive messages via webhooks.

        Args:
            payload (Dict[str, Any]): Raw webhook payload delivered by the
                platform (already deserialized from JSON).

        Returns:
            Optional[Message]: A parsed :class:`Message` if the payload
                contains a valid inbound message, or ``None`` if the payload
                should be ignored (e.g. a verification challenge or bot event).
        """
        # Default implementation - override for webhook support
        return None

    def set_handler(self, handler: MessageHandler) -> None:
        """Register the callback that receives inbound messages.

        Args:
            handler (MessageHandler): Async callable that accepts a
                :class:`Message` and processes it.
        """
        self._handler = handler

    async def start_listening(self) -> None:
        """Start the background listener task for incoming messages.

        Creates an asyncio task that calls :meth:`_listen`. If the adapter
        is already running, this method does nothing.
        """
        if self._running:
            return

        self._running = True
        self._listener_task = asyncio.create_task(
            self._listen(),
            name=f"channel-{self.channel_name}-listen"
        )

    async def stop_listening(self) -> None:
        """Stop the background listener task and wait for it to finish.

        Cancels the listener task created by :meth:`start_listening` and
        suppresses the expected :class:`asyncio.CancelledError`.
        """
        self._running = False

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

    async def _listen(self) -> None:
        """Internal listener loop. Override for polling-based adapters.

        The default implementation sleeps in a loop and does nothing. Subclasses
        that require long-polling or periodic polling should override this method
        to fetch new messages and call :meth:`_dispatch` for each one.
        """
        # Default implementation does nothing
        # Override for platforms that need polling
        while self._running:
            await asyncio.sleep(1)

    async def _dispatch(self, message: Message) -> None:
        """Forward a received message to the registered handler.

        Calls the handler set via :meth:`set_handler`. Exceptions raised by
        the handler are caught and logged so that a single bad message does
        not crash the listener loop.

        Args:
            message (Message): The inbound message to dispatch.
        """
        if self._handler:
            try:
                await self._handler(message)
            except Exception as e:
                # Log but don't crash
                import logging
                logging.getLogger(f"pyclopse.channels.{self.channel_name}").error(
                    f"Error handling message: {e}"
                )
