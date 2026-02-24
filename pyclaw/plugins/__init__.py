"""Plugin system for pyclaw."""

from pyclaw.plugins.types import PluginType, PluginState, PluginMetadata, PluginInfo
from pyclaw.plugins.base import Plugin, ChannelPlugin
from pyclaw.plugins.registry import PluginRegistry
from pyclaw.plugins.loader import PluginLoader, BuiltinPluginLoader
from pyclaw.plugins.loaders import (
    MultiTypePluginLoader,
    PythonPluginLoader,
    HTTPPluginLoader,
    SubprocessPluginLoader,
    JSONPluginLoader,
)
from pyclaw.plugins.hooks import HookPhase, HookRegistry
from pyclaw.plugins.channels.telegram import TelegramPlugin
from pyclaw.plugins.channels.discord import DiscordPlugin


__all__ = [
    "Plugin",
    "PluginType",
    "PluginState",
    "PluginMetadata",
    "PluginInfo",
    "ChannelPlugin",
    "TelegramPlugin",
    "DiscordPlugin",
    "HookPhase",
    "HookRegistry",
    "PluginRegistry",
    "PluginLoader",
    "BuiltinPluginLoader",
    "MultiTypePluginLoader",
    "PythonPluginLoader",
    "HTTPPluginLoader",
    "SubprocessPluginLoader",
    "JSONPluginLoader",
]
