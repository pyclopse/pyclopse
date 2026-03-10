"""Configuration schema definitions using Pydantic."""

from pydantic import BaseModel, Field, field_validator, ConfigDict, AliasChoices
from typing import Optional, List, Dict, Any
from enum import Enum
import os

from pyclaw.secrets.models import SecretsConfig


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
    memory_limit: Optional[str] = Field(default=None, validation_alias="memoryLimit")
    cpu_limit: Optional[float] = Field(default=None, validation_alias="cpuLimit")
    pids_limit: Optional[int] = Field(default=None, validation_alias="pidsLimit")
    read_only: bool = Field(default=True, validation_alias="readOnly")
    tmp_size: Optional[int] = Field(default=None, validation_alias="tmpSize")  # MB
    allowed_volumes: List[str] = Field(default_factory=list, validation_alias="allowedVolumes")


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
    # Global user denylist — always blocked regardless of allowed_users per channel
    denied_users: List[int] = Field(default_factory=list, validation_alias="deniedUsers")


class ClawVaultConfig(BaseModel):
    """ClawVault CLI wrapper config."""
    vault_path: str = "~/.claw/vault"
    enabled: bool = True


class FileMemoryConfig(BaseModel):
    """
    File-based per-agent memory backend configuration.

    Each agent stores memory under its own directory::

        ~/.pyclaw/agents/{agent_name}/
            MEMORY.md        # curated notes; injected into sessions
            memory/
                2026-03-10.md  # daily journal written by memory tools
    """
    # Inject MEMORY.md content into each session's system prompt
    inject_curated: bool = Field(
        default=True,
        validation_alias=AliasChoices("inject_curated", "injectCurated"),
    )


class EmbeddingConfig(BaseModel):
    """
    Embedding model configuration for vector-based memory search.

    When ``enabled = True`` the memory backend indexes every stored entry and
    uses cosine similarity at search time.  Falls back to keyword scoring when
    disabled or when a queried key has no vector yet.

    Example (OpenAI)::

        memory:
          embedding:
            enabled: true
            provider: openai
            model: text-embedding-3-small
            apiKey: ${OPENAI_API_KEY}

    Example (local llama.cpp / Ollama)::

        memory:
          embedding:
            enabled: true
            provider: local
            model: nomic-embed-text
            baseUrl: http://localhost:11434
    """
    enabled: bool = False
    provider: str = "openai"        # openai | gemini | local
    model: str = ""                 # "" → provider default
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("api_key", "apiKey"),
    )
    base_url: str = Field(
        default="",
        validation_alias=AliasChoices("base_url", "baseUrl"),
    )
    # Override embedding dimensionality (0 = use provider default)
    dimensions: int = 0


class MemoryConfig(BaseModel):
    """Memory configuration."""
    # "file" uses the built-in markdown journal; "clawvault" uses ClawVault CLI
    backend: str = "file"
    file: FileMemoryConfig = Field(default_factory=FileMemoryConfig)
    clawvault: ClawVaultConfig = Field(default_factory=ClawVaultConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


class ProviderConfig(BaseModel):
    """Base provider configuration."""
    enabled: bool = True
    api_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("api_key", "apiKey"))
    default_model: Optional[str] = Field(default=None, validation_alias=AliasChoices("default_model", "defaultModel"))

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


class MiniMaxProviderConfig(ProviderConfig):
    """MiniMax provider configuration."""
    base_url: str = "https://api.minimax.chat/v1"


class ProvidersConfig(BaseModel):
    """Providers configuration."""
    openai: Optional[OpenAIProviderConfig] = None
    anthropic: Optional[AnthropicProviderConfig] = None
    google: Optional[GoogleProviderConfig] = None
    fastagent: Optional[FastAgentProviderConfig] = None
    minimax: Optional[MiniMaxProviderConfig] = None


class HeartbeatConfig(BaseModel):
    """Heartbeat configuration for an agent."""
    enabled: bool = True
    every: str = "30m"
    prompt: str = "Check for any important updates."
    active_hours: Optional[Dict[str, str]] = Field(default=None, validation_alias="activeHours")


class ToolsConfig(BaseModel):
    """
    Tools configuration for an agent.

    Examples:
        tools:
          profile: coding           # named profile
          allow: [web_search]       # add on top of profile
          deny: [bash]              # remove from profile

        tools:
          allow: [bash, read_file]  # explicit allowlist (no profile)

        tools:
          profile: full             # all tools
    """
    enabled: bool = True
    profile: Optional[str] = None           # minimal | coding | web | messaging | full
    allow: List[str] = Field(default_factory=list)   # tool names or group: prefixes
    deny: List[str] = Field(default_factory=list)
    # Legacy field kept for backwards compat
    allowlist: List[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Agent configuration."""
    model_config = ConfigDict(extra="allow")  # Allow additional fields like use_fastagent, workflow

    name: str = "Assistant"
    model: str = "openai/gpt-4"
    max_tokens: int = Field(4096, validation_alias=AliasChoices("max_tokens", "maxTokens"))
    temperature: float = 0.7
    system_prompt: str = Field("You are a helpful assistant.", validation_alias=AliasChoices("system_prompt", "systemPrompt"))
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    # FastAgent-specific config (extra fields allowed)
    use_fastagent: bool = Field(default=False, validation_alias=AliasChoices("use_fastagent", "useFastagent"))
    workflow: Optional[str] = None
    agents: Optional[List[str]] = None
    mcp_servers: Optional[List[str]] = Field(default=None, validation_alias=AliasChoices("mcp_servers", "mcpServers"))
    # Response post-processing
    show_thinking: bool = Field(
        default=False,
        validation_alias=AliasChoices("show_thinking", "showThinking"),
    )
    # Typing indicator mode: "none" | "typing" (send channel typing action while processing)
    typing_mode: str = Field(
        default="none",
        validation_alias=AliasChoices("typing_mode", "typingMode"),
    )


class AgentsConfig(BaseModel):
    """Agents configuration (dict of agent configs)."""
    model_config = ConfigDict(extra="allow")  # Allow additional agents
    
    # Note: No default agent - all agents must be defined in config file
    # The extra="allow" setting permits any number of named agents


class NodeConfig(BaseModel):
    """Node configuration for peer-to-peer communication."""
    enabled: bool = False
    node_id: Optional[str] = Field(default=None, validation_alias="nodeId")
    whitelist: List[str] = Field(default_factory=list)
    require_approval: bool = Field(True, validation_alias="requireApproval")
    secret_key: Optional[str] = Field(default=None, validation_alias="secretKey")

    @field_validator("secret_key", mode="before")
    @classmethod
    def resolve_env_var(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            return os.environ.get(var_name)
        return v


class JobsConfig(BaseModel):
    """Jobs (cron) configuration."""
    enabled: bool = True
    persist_file: str = Field("~/.pyclaw/jobs.json", validation_alias="persistFile")


class TodosConfig(BaseModel):
    """TODO registry configuration."""
    enabled: bool = True
    persist_file: str = Field(
        "~/.pyclaw/todos.json",
        validation_alias=AliasChoices("persist_file", "persistFile"),
    )


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = True
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    allowed_users: List[int] = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: List[int] = Field(default_factory=list, validation_alias="deniedUsers")
    # Map topic names → Telegram forum topic IDs for group chats with topics
    topics: Dict[str, int] = Field(default_factory=dict)
    # Send typing indicator while agent is processing
    typing_indicator: bool = Field(default=True, validation_alias="typingIndicator")

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
    allowed_users: List[str] = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: List[str] = Field(default_factory=list, validation_alias="deniedUsers")
    # Reply in thread when message is part of a Slack thread
    threading: bool = True
    # Slack channel ID to post pulse/heartbeat messages to (e.g. "C1234567890")
    pulse_channel: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("pulse_channel", "pulseChannel")
    )

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
    allowed_users: List[str] = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: List[str] = Field(default_factory=list, validation_alias="deniedUsers")

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


class PluginType(str, Enum):
    """Types of plugins supported by pyclaw."""
    PYTHON = "python"       # Native Python plugin (loaded directly)
    HTTP = "http"           # HTTP/RPC plugin (separate process)
    SUBPROCESS = "subprocess"  # stdio communication (any language)
    JSON = "json"           # Config-only plugins (no code)


class PluginEntryConfig(BaseModel):
    """Single plugin entry configuration."""
    enabled: bool = True
    type: PluginType = PluginType.PYTHON
    # For python type
    path: Optional[str] = None
    module: Optional[str] = None
    # For http type
    url: Optional[str] = None
    health: Optional[str] = None
    # For subprocess type
    command: Optional[str] = None
    protocol: str = "json"  # json, text
    # For json type
    config: Dict[str, Any] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Plugins configuration."""
    enabled: bool = True
    auto_enable: bool = Field(True, validation_alias="autoEnable")
    entries: Dict[str, PluginEntryConfig] = Field(default_factory=dict)
    # List of channel plugin specs in "module.path:ClassName" format.
    # Entry-point plugins (pyclaw.channels group) are always discovered
    # automatically; this list adds to them.
    channels: List[str] = Field(default_factory=list)


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


class BrowserAutomationConfig(BaseModel):
    """Browser automation configuration."""
    enabled: bool = False
    headless: bool = True
    slow_mo: int = 0
    timeout: int = 30000
    viewport_width: int = 1280
    viewport_height: int = 720


class MemoryQmdPathConfig(BaseModel):
    """Memory QMD path configuration."""
    path: str
    name: str


class MemoryQmdConfig(BaseModel):
    """Memory QMD configuration."""
    enabled: bool = False
    paths: List[MemoryQmdPathConfig] = Field(default_factory=list)


class SessionsConfig(BaseModel):
    """Session management configuration."""
    persist_dir: str = Field(
        default="~/.pyclaw/sessions",
        validation_alias=AliasChoices("persist_dir", "persistDir"),
    )
    # Sessions idle longer than ttl_hours will be cleaned up by the reaper
    ttl_hours: int = Field(
        default=24,
        validation_alias=AliasChoices("ttl_hours", "ttlHours"),
    )
    # How often the reaper runs (minutes)
    reaper_interval_minutes: int = Field(
        default=60,
        validation_alias=AliasChoices("reaper_interval_minutes", "reaperIntervalMinutes"),
    )


class ConcurrencyConfig(BaseModel):
    """Per-model concurrency limits.

    Example:
        concurrency:
          default: 3
          models:
            MiniMax-M2.5: 3
            gpt-4: 5
            passthrough: 100
    """
    default: int = 3
    models: Dict[str, int] = Field(default_factory=dict)


class GatewayConfig(BaseModel):
    """Gateway server configuration."""
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_port: int = Field(default=8081, validation_alias=AliasChoices("mcp_port", "mcpPort"))
    debug: bool = False
    log_level: str = "info"
    webhook_url: Optional[str] = Field(default=None, validation_alias="webhookUrl")
    cors_origins: List[str] = Field(default_factory=lambda: ["*"], validation_alias="corsOrigins")
    # Additional skill search directories (on top of ~/.pyclaw/skills/)
    skills_dirs: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("skills_dirs", "skillsDirs"),
    )


class Config(BaseModel):
    """Root configuration model."""
    version: str = "1.0"
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    todos: TodosConfig = Field(default_factory=TodosConfig)
    nodes: NodeConfig = Field(default_factory=NodeConfig)
    browser: BrowserAutomationConfig = Field(default_factory=BrowserAutomationConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    memory_qmd: MemoryQmdConfig = Field(
        default_factory=MemoryQmdConfig,
        validation_alias="memoryQmd"
    )
