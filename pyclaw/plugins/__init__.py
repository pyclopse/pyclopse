"""Plugin system for pyclaw."""

from pyclaw.plugins.types import PluginType, PluginState, PluginMetadata, PluginInfo
from pyclaw.plugins.base import Plugin, ChannelPlugin
from pyclaw.plugins.registry import PluginRegistry
from pyclaw.plugins.loader import PluginLoader, BuiltinPluginLoader
from pyclaw.plugins.hooks import HookPhase, HookRegistry


__all__ = [
    "Plugin",
    "PluginType",
    "PluginState",
    "PluginMetadata",
    "PluginInfo",
    "ChannelPlugin",
    "HookPhase",
    "HookRegistry",
    "PluginRegistry",
    "PluginLoader",
    "BuiltinPluginLoader",
]
