"""Channel registry and exports."""
from typing import Dict, Type, Optional
from .base import ChannelAdapter
from .telegram import TelegramAdapter
from .discord import DiscordAdapter, DiscordWebhookAdapter
from .slack import SlackAdapter
from .whatsapp import WhatsAppAdapter
from .signal import SignalAdapter
from .line import LineAdapter
from .imessage import IMessageAdapter
from .googlechat import GoogleChatAdapter


# Channel registry
CHANNEL_REGISTRY: Dict[str, Type[ChannelAdapter]] = {
    "telegram": TelegramAdapter,
    "discord": DiscordAdapter,
    "discord_webhook": DiscordWebhookAdapter,
    "slack": SlackAdapter,
    "whatsapp": WhatsAppAdapter,
    "signal": SignalAdapter,
    "line": LineAdapter,
    "imessage": IMessageAdapter,
    "googlechat": GoogleChatAdapter,
}


def register_channel(name: str):
    """Decorator to register a channel adapter."""
    def decorator(cls: Type[ChannelAdapter]):
        CHANNEL_REGISTRY[name] = cls
        return cls
    return decorator


def get_channel(name: str, config: dict) -> ChannelAdapter:
    """
    Get a channel adapter instance by name.
    
    Args:
        name: Channel name (e.g., 'telegram', 'discord')
        config: Channel configuration dict
        
    Returns:
        ChannelAdapter instance
        
    Raises:
        ValueError: If channel name is unknown
    """
    if name not in CHANNEL_REGISTRY:
        raise ValueError(
            f"Unknown channel: {name}. "
            f"Available: {list(CHANNEL_REGISTRY.keys())}"
        )
    return CHANNEL_REGISTRY[name](config)


def list_channels() -> list:
    """List all registered channel names."""
    return list(CHANNEL_REGISTRY.keys())


__all__ = [
    "ChannelAdapter",
    "Message",
    "MessageTarget", 
    "MediaAttachment",
    "TelegramAdapter",
    "DiscordAdapter",
    "DiscordWebhookAdapter",
    "SlackAdapter",
    "WhatsAppAdapter",
    "SignalAdapter",
    "LineAdapter",
    "IMessageAdapter",
    "GoogleChatAdapter",
    "register_channel",
    "get_channel",
    "list_channels",
    "CHANNEL_REGISTRY",
]
