"""Configuration system for pyclawops.

This module provides YAML-based configuration with Pydantic validation.

Example:
    >>> from pyclawops.config import load_config
    >>> 
    >>> # Load config from default location
    >>> config = load_config()
    >>> 
    >>> # Or load from specific path
    >>> config = load_config("~/my-config.yaml")
    >>> 
    >>> # Access config values
    >>> print(config.gateway.host)
    >>> print(config.security.exec_approvals.mode)
"""

from .schema import (
    Config,
    GatewayConfig,
    SecurityConfig,
    SecurityMode,
    ExecApprovalsConfig,
    SandboxConfig,
    AuditConfig,
    MemoryConfig,
    ClawVaultConfig,
    ProvidersConfig,
    ProviderConfig,
    AgentsConfig,
    AgentConfig,
    ToolsConfig,
    JobsConfig,
    ChannelsConfig,
    TelegramConfig,
    DiscordConfig,
    SlackConfig,
    WhatsAppConfig,
    PluginsConfig,
    HooksConfig,
    TUIConfig,
    MemoryQmdConfig,
)
from .loader import (
    ConfigLoader,
    load_config,
    load_yaml,
    save_yaml,
    create_default_config,
    find_config_file,
)

__all__ = [
    # Schema
    "Config",
    "GatewayConfig",
    "SecurityConfig",
    "SecurityMode",
    "ExecApprovalsConfig",
    "SandboxConfig",
    "AuditConfig",
    "MemoryConfig",
    "ClawVaultConfig",
    "ProvidersConfig",
    "ProviderConfig",
    "AgentsConfig",
    "AgentConfig",
    "ToolsConfig",
    "JobsConfig",
    "ChannelsConfig",
    "TelegramConfig",
    "DiscordConfig",
    "SlackConfig",
    "WhatsAppConfig",
    "PluginsConfig",
    "HooksConfig",
    "TUIConfig",
    "MemoryQmdConfig",
    # Loader
    "ConfigLoader",
    "load_config",
    "load_yaml",
    "save_yaml",
    "create_default_config",
    "find_config_file",
]
