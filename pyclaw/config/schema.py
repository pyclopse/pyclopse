"""Configuration schema definitions using Pydantic."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from enum import Enum
import os


class SecurityMode(str, Enum):
    """Execution approval mode."""
    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"
    ALL = "all"
    NONE = "none"


class ExecApprovalsConfig(BaseModel):
    """Execution approval configuration."""
    mode: SecurityMode = SecurityMode.ALLOWLIST
    safe_bins: List[str] = Field(default_factory=list)
    always_approve: List[str] = Field(default_factory=list)


class DockerSandboxConfig(BaseModel):
    """Docker sandbox configuration."""
    image: str = "pyclaw-sandbox:latest"
    network: str = "none"


class SandboxConfig(BaseModel):
    """Sandbox configuration."""
    enabled: bool = True
    type: str = "none"  # docker, none
    docker: Optional[DockerSandboxConfig] = None


class AuditConfig(BaseModel):
    """Audit logging configuration."""
    enabled: bool = True
    log_file: str = "~/.pyclaw/logs/audit.log"
    retention_days: int = 90


class SecurityConfig(BaseModel):
    """Security configuration."""
    exec_approvals: ExecApprovalsConfig = Field(
        default_factory=ExecApprovalsConfig,
        validation_alias="execApprovals"
    )
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)


class ClawVaultConfig(BaseModel):
    """ClawVault CLI wrapper config."""
    vault_path: str = "~/.claw/vault"
    enabled: bool = True


class MemoryConfig(BaseModel):
    """Memory configuration."""
    backend: str = "clawvault"
    clawvault: ClawVaultConfig = Field(default_factory=ClawVaultConfig)


class ProviderConfig(BaseModel):
    """Base provider configuration."""
    enabled: bool = True
    api_key: Optional[str] = Field(default=None, validation_alias="apiKey")
    default_model: Optional[str] = Field(default=None, validation_alias="defaultModel")

    @field_validator("api_key", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        """Resolve environment variables in format ${VAR_NAME}."""
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class OpenAIProviderConfig(ProviderConfig):
    """OpenAI provider configuration."""
    pass


class AnthropicProviderConfig(ProviderConfig):
    """Anthropic provider configuration."""
    pass


class GoogleProviderConfig(ProviderConfig):
    """Google provider configuration."""
    pass


class FastAgentProviderConfig(ProviderConfig):
    """FastAgent provider configuration."""
    url: str = "http://localhost:8000"


class ProvidersConfig(BaseModel):
    """Providers configuration."""
    openai: Optional[OpenAIProviderConfig] = None
    anthropic: Optional[AnthropicProviderConfig] = None
    google: Optional[GoogleProviderConfig] = None
    fastagent: Optional[FastAgentProviderConfig] = None


class HeartbeatConfig(BaseModel):
    """Heartbeat configuration for an agent."""
    enabled: bool = True
    every: str = "30m"
    prompt: str = "Check for any important updates."
    active_hours: Optional[Dict[str, str]] = Field(default=None, validation_alias="activeHours")


class ToolsConfig(BaseModel):
    """Tools configuration for an agent."""
    enabled: bool = True
    allowlist: List[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Agent configuration."""
    name: str = "Assistant"
    model: str = "openai/gpt-4"
    max_tokens: int = Field(4096, validation_alias="maxTokens")
    temperature: float = 0.7
    system_prompt: str = Field("You are a helpful assistant.", validation_alias="systemPrompt")
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class AgentsConfig(BaseModel):
    """Agents configuration (dict of agent configs)."""
    default: AgentConfig = Field(default_factory=AgentConfig)
    # Additional agents can be added as dict items


class JobsConfig(BaseModel):
    """Jobs (cron) configuration."""
    enabled: bool = True
    persist_file: str = Field("~/.pyclaw/jobs.json", validation_alias="persistFile")


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = True
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    allowed_users: List[int] = Field(default_factory=list, validation_alias="allowedUsers")

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class DiscordConfig(BaseModel):
    """Discord channel configuration."""
    enabled: bool = False
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    guilds: List[Dict[str, str]] = Field(default_factory=list)

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class SlackConfig(BaseModel):
    """Slack channel configuration."""
    enabled: bool = False
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    signing_secret: Optional[str] = Field(default=None, validation_alias="signingSecret")

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    phone_id: Optional[str] = Field(default=None, validation_alias="phoneId")
    access_token: Optional[str] = Field(default=None, validation_alias="accessToken")

    @field_validator("access_token", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class ChannelsConfig(BaseModel):
    """Channels configuration."""
    telegram: Optional[TelegramConfig] = None
    discord: Optional[DiscordConfig] = None
    slack: Optional[SlackConfig] = None
    whatsapp: Optional[WhatsAppConfig] = None


class PluginEntryConfig(BaseModel):
    """Single plugin entry configuration."""
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Plugins configuration."""
    enabled: bool = True
    auto_enable: bool = Field(True, validation_alias="autoEnable")
    entries: Dict[str, PluginEntryConfig] = Field(default_factory=dict)


class HookEntryConfig(BaseModel):
    """Single hook entry configuration."""
    enabled: bool = False


class HooksConfig(BaseModel):
    """Hooks configuration."""
    internal: bool = True
    external: bool = True
    entries: Dict[str, HookEntryConfig] = Field(default_factory=dict)


class TUIConfig(BaseModel):
    """TUI configuration."""
    enabled: bool = True
    theme: str = "dark"
    key_bindings: Dict[str, str] = Field(default_factory=dict, validation_alias="keyBindings")


class MemoryQmdPathConfig(BaseModel):
    """Memory QMD path configuration."""
    path: str
    name: str


class MemoryQmdConfig(BaseModel):
    """Memory QMD configuration."""
    enabled: bool = False
    paths: List[MemoryQmdPathConfig] = Field(default_factory=list)


class GatewayConfig(BaseModel):
    """Gateway server configuration."""
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    log_level: str = "info"


class Config(BaseModel):
    """Root configuration model."""
    version: str = "1.0"
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    memory_qmd: MemoryQmdConfig = Field(
        default_factory=MemoryQmdConfig,
        validation_alias="memoryQmd"
    )
