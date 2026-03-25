"""Channel registry, adapters, and plugin system."""
from typing import Dict, Type, Optional
from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment
from .plugin import ChannelPlugin, GatewayHandle
from .loader import load_all, load_from_specs, discover_entry_points


# Legacy adapter registry (ChannelAdapter subclasses, not yet wired to gateway)
CHANNEL_REGISTRY: Dict[str, Type[ChannelAdapter]] = {}


def register_channel(name: str):
    """Decorator to register a legacy ChannelAdapter by name."""
    def decorator(cls: Type[ChannelAdapter]):
        CHANNEL_REGISTRY[name] = cls
        return cls
    return decorator


def get_channel(name: str, config: dict) -> ChannelAdapter:
    """Instantiate a legacy ChannelAdapter by name."""
    if name not in CHANNEL_REGISTRY:
        raise ValueError(
            f"Unknown channel: {name}. "
            f"Available: {list(CHANNEL_REGISTRY.keys())}"
        )
    return CHANNEL_REGISTRY[name](config)


def list_channels() -> list:
    """List all registered legacy channel names."""
    return list(CHANNEL_REGISTRY.keys())


__all__ = [
    # Base adapter (legacy)
    "ChannelAdapter",
    "Message",
    "MessageTarget",
    "MediaAttachment",
    # Plugin system
    "ChannelPlugin",
    "GatewayHandle",
    "load_all",
    "load_from_specs",
    "discover_entry_points",
    # Legacy registry
    "register_channel",
    "get_channel",
    "list_channels",
    "CHANNEL_REGISTRY",
]
