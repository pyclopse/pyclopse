"""Configuration schema definitions using Pydantic."""

from pydantic import BaseModel, Field, ConfigDict, AliasChoices, model_validator
from typing import Optional, List, Dict, Any
from enum import Enum
from pyclaw.reflect import reflect_system

from pyclaw.secrets.models import SecretsConfig
from pyclaw.memory.vault.config import VaultConfig as _VaultConfig


class QueueMode(str, Enum):
    """Message queue processing mode."""
    FOLLOWUP = "followup"
    COLLECT = "collect"
    INTERRUPT = "interrupt"
    STEER = "steer"
    STEER_BACKLOG = "steer-backlog"
    STEER_PLUS_BACKLOG = "steer+backlog"
    QUEUE = "queue"


class DropPolicy(str, Enum):
    """Queue overflow drop policy."""
    OLD = "old"
    NEW = "new"
    SUMMARIZE = "summarize"


class QueueModeByChannel(BaseModel):
    """Per-channel queue mode overrides (OC QueueModeByProvider)."""
    telegram: Optional[QueueMode] = None
    slack: Optional[QueueMode] = None
    discord: Optional[QueueMode] = None
    whatsapp: Optional[QueueMode] = None
    signal: Optional[QueueMode] = None
    imessage: Optional[QueueMode] = None
    googlechat: Optional[QueueMode] = None


class QueueConfig(BaseModel):
    """Per-agent message queue configuration."""
    mode: QueueMode = QueueMode.COLLECT
    debounce_ms: int = Field(
        default=300,
        validation_alias=AliasChoices("debounce_ms", "debounceMs"),
    )
    cap: int = 20
    drop: DropPolicy = DropPolicy.OLD
    by_channel: QueueModeByChannel = Field(
        default_factory=QueueModeByChannel,
        validation_alias=AliasChoices("by_channel", "byChannel"),
    )


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
    # Maps this provider to a FastAgent provider name (e.g. "generic" for OpenAI-compatible
    # endpoints).  When set, agent model strings like "minimax/MiniMax-M2.5" are translated
    # to "<fastagent_provider>.<model>" (e.g. "generic.MiniMax-M2.5") before being handed
    # to FastAgent, and the provider credentials are injected as
    # <FASTAGENT_PROVIDER>_API_KEY / <FASTAGENT_PROVIDER>_BASE_URL env vars.
    fastagent_provider: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("fastagent_provider", "fastagentProvider"),
    )



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


class ModelConfig(BaseModel):
    """Per-model configuration within a provider."""
    priority: int = 1
    enabled: bool = True
    concurrency: int = 3


class UsageThrottleConfig(BaseModel):
    """Usage-based throttle thresholds (% used).

    When usage reaches the threshold for a priority level, new requests at
    that priority are blocked.  ``critical`` (chat) is never throttled.

    Example::

        throttle:
          background: 70   # pause vault ingestion at 70 % used
          normal: 90       # pause scheduled jobs at 90 % used
    """
    background: int = 70
    normal: int = 90


class UsageConfig(BaseModel):
    """Provider usage monitoring configuration.

    Polls the provider's usage endpoint on a configurable interval and
    throttles requests based on configurable percentage thresholds.

    All fields except ``endpoint`` are optional.  If ``api_key`` is omitted,
    the parent provider's ``api_key`` is used.

    Example (z.ai)::

        usage:
          enabled: true
          endpoint: "https://api.z.ai/v1/usage"
          used_path: "used"
          total_path: "total"
          check_interval: 300
          throttle:
            background: 70
            normal: 90

    Example (MiniMax with extra query param)::

        usage:
          enabled: true
          endpoint: "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
          params:
            GroupId: "1234567890"
          total_path: "model_remains.0.current_interval_total_count"
          remaining_path: "model_remains.0.current_interval_usage_count"
    """
    enabled: bool = True
    endpoint: str
    # Auth token — uses the provider's api_key if omitted.
    # Supports ${SECRET_NAME} syntax (resolved at load time).
    api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("api_key", "apiKey"),
    )
    # Extra query parameters passed to the endpoint
    params: Dict[str, str] = Field(default_factory=dict)
    # Polling interval in seconds (default: 5 minutes)
    check_interval: int = Field(
        default=300,
        validation_alias=AliasChoices("check_interval", "checkInterval"),
    )

    # Response parsing — provide one of:
    #   percent_path                     → direct 0-100 percentage
    #   total_path + remaining_path      → (total - remaining) / total × 100
    #   total_path + used_path           → used / total × 100
    # Dot-notation; integer segments treated as list indices.
    percent_path: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("percent_path", "percentPath"),
    )
    total_path: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("total_path", "totalPath"),
    )
    used_path: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("used_path", "usedPath"),
    )
    remaining_path: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("remaining_path", "remainingPath"),
    )

    throttle: UsageThrottleConfig = Field(default_factory=UsageThrottleConfig)


class GenericProviderConfig(ProviderConfig):
    """Configuration for any OpenAI-compatible provider endpoint."""
    api_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("api_url", "apiUrl", "base_url", "baseUrl"),
    )
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
    # Optional usage monitoring — omit to disable
    usage: Optional[UsageConfig] = None


# Backwards-compatible alias
MiniMaxProviderConfig = GenericProviderConfig


class ProvidersConfig(BaseModel):
    """Providers configuration.

    Named providers (openai, anthropic, google, fastagent, minimax) are typed
    fields.  Any additional OpenAI-compatible provider (e.g. ``zai``, ``groq``)
    can be added directly in YAML without any code change — it is validated into
    a ``GenericProviderConfig`` and works identically to minimax.
    """
    model_config = ConfigDict(extra="allow")

    openai: Optional[OpenAIProviderConfig] = None
    anthropic: Optional[AnthropicProviderConfig] = None
    google: Optional[GoogleProviderConfig] = None
    fastagent: Optional[FastAgentProviderConfig] = None
    minimax: Optional[GenericProviderConfig] = None

    @model_validator(mode="after")
    def _coerce_extra_providers(self) -> "ProvidersConfig":
        """Validate extra provider entries (e.g. zai) into GenericProviderConfig."""
        if self.model_extra:
            for name, value in self.model_extra.items():
                if isinstance(value, dict):
                    self.model_extra[name] = GenericProviderConfig.model_validate(value)
        return self


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
    # Output token limit. MiniMax-M2.5 supports up to ~196,608; default covers reasoning budget.
    max_tokens: int = Field(16384, validation_alias=AliasChoices("max_tokens", "maxTokens"))
    temperature: float = 0.7
    # Nucleus sampling — None means provider default
    top_p: Optional[float] = Field(default=None, validation_alias=AliasChoices("top_p", "topP"))
    # Maximum tool-call iterations per turn (FastAgent default: 99)
    max_iterations: Optional[int] = Field(default=None, validation_alias=AliasChoices("max_iterations", "maxIterations"))
    # Whether to allow parallel tool calls (FastAgent default: True)
    parallel_tool_calls: Optional[bool] = Field(default=None, validation_alias=AliasChoices("parallel_tool_calls", "parallelToolCalls"))
    # Seconds to wait for streaming completion (FastAgent default: 300)
    streaming_timeout: Optional[float] = Field(default=None, validation_alias=AliasChoices("streaming_timeout", "streamingTimeout"))
    system_prompt: str = Field("You are a helpful assistant.", validation_alias=AliasChoices("system_prompt", "systemPrompt"))
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
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
    # Generic request parameters forwarded to the LLM provider.
    # Known FastAgent params (top_p, temperature, max_iterations, etc.) are forwarded
    # directly to RequestParams; anything else goes into extra_body for the raw API call.
    # Example: request_params: {reasoning_split: true, frequency_penalty: 0.1}
    request_params: Optional[Dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("request_params", "requestParams"),
    )
    # FastAgent model-level settings applied at runner init time.
    # reasoning_effort: off | none | minimal | low | medium | high | xhigh | max | auto
    reasoning_effort: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("reasoning_effort", "reasoningEffort"),
    )
    # text_verbosity: low | medium | high
    text_verbosity: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("text_verbosity", "textVerbosity"),
    )
    # service_tier: fast | flex
    service_tier: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("service_tier", "serviceTier"),
    )
    # Model fallback chain — ordered list of fallback models tried on error.
    # Example: fallbacks: [claude-sonnet, gpt-4o]
    fallbacks: List[str] = Field(
        default_factory=list,
    )
    # Model context window size (tokens). Used by /status to show context utilisation.
    # Leave unset if unknown; pyclaw will estimate from history file size.
    # Examples: 200000 (Claude), 128000 (GPT-4o), 196608 (MiniMax-M2.5)
    context_window: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("context_window", "contextWindow"),
    )
    # Message queue configuration — controls how rapid inbound messages are handled
    # while the agent is processing a previous message.
    queue: QueueConfig = Field(default_factory=QueueConfig)
    # Additional skill search directories for this agent only.
    # Skills here override same-named global skills. Searched after ~/.pyclaw/skills/
    # and ~/.pyclaw/agents/{name}/skills/ but before any gateway.skills_dirs paths.
    skills_dirs: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("skills_dirs", "skillsDirs"),
    )
    # A2A (Agent-to-Agent) protocol configuration for this agent.
    a2a: Optional[A2AAgentConfig] = Field(
        default=None,
        validation_alias=AliasChoices("a2a"),
    )
    # Vault memory configuration. Omit (or leave as {}) → enabled with defaults.
    # Set to null in YAML → vault disabled for this agent.
    vault: Optional[_VaultConfig] = Field(
        default_factory=_VaultConfig,
        validation_alias=AliasChoices("vault"),
    )

    # ── Workflow: orchestrator / iterative_planner ────────────────────────────
    # workflow: orchestrator
    #   agents: [researcher, writer]
    #   planType: full          # "full" (one upfront plan) | "iterative" (step-by-step)
    #   planIterations: 5       # max planning iterations; -1 = unlimited (iterative_planner default)
    plan_type: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("plan_type", "planType"),
    )
    plan_iterations: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("plan_iterations", "planIterations"),
    )

    # ── Workflow: evaluator_optimizer ─────────────────────────────────────────
    # workflow: evaluator_optimizer
    #   generator: drafter        # agent that produces responses
    #   evaluator: critic         # agent that scores responses
    #   minRating: GOOD           # EXCELLENT | GOOD | FAIR | POOR
    #   maxRefinements: 3
    #   refinementInstruction: "Improve based on the critique."
    generator: Optional[str] = None
    evaluator: Optional[str] = None
    min_rating: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("min_rating", "minRating"),
    )
    max_refinements: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("max_refinements", "maxRefinements"),
    )
    refinement_instruction: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("refinement_instruction", "refinementInstruction"),
    )

    # ── Workflow: maker (K-voting) ────────────────────────────────────────────
    # workflow: maker
    #   worker: classifier        # agent to sample from
    #   k: 3                      # consensus margin required
    #   maxSamples: 50            # give up after this many attempts
    #   matchStrategy: normalized # "exact" | "normalized" | "structured"
    #   redFlagMaxLength: 200     # discard responses longer than N chars
    worker: Optional[str] = None
    k: Optional[int] = None
    max_samples: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("max_samples", "maxSamples"),
    )
    match_strategy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("match_strategy", "matchStrategy"),
    )
    red_flag_max_length: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("red_flag_max_length", "redFlagMaxLength"),
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



class JobsConfig(BaseModel):
    """Jobs (cron) configuration."""
    model_config = ConfigDict(populate_by_name=True)
    enabled: bool = True
    agents_dir: str = Field(
        "~/.pyclaw/agents",
        validation_alias=AliasChoices("agents_dir", "agentsDir"),
    )
    default_timezone: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("default_timezone", "defaultTimezone"),
    )


class TodosConfig(BaseModel):
    """TODO registry configuration."""
    enabled: bool = True
    persist_file: str = Field(
        "~/.pyclaw/todos.json",
        validation_alias=AliasChoices("persist_file", "persistFile"),
    )


class TelegramBotConfig(BaseModel):
    """Per-bot Telegram configuration within a multi-bot setup.

    Fields left as ``None`` inherit the value from the parent ``TelegramConfig``.
    """
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    # agent_id this bot routes messages to; None = use first configured agent
    agent: Optional[str] = None
    allowed_users: Optional[List[int]] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[List[int]] = Field(default=None, validation_alias="deniedUsers")
    typing_indicator: Optional[bool] = Field(default=None, validation_alias="typingIndicator")
    streaming: Optional[bool] = None

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
    # Stream response by editing a single message in place
    streaming: bool = Field(default=False)
    # Multi-bot: named bots, each routing to a specific agent
    bots: Dict[str, TelegramBotConfig] = Field(default_factory=dict)

    def effective_config_for_bot(self, name: str) -> TelegramBotConfig:
        """Return a fully-resolved config for the named bot, inheriting parent defaults.

        Fields that are ``None`` on the named bot entry are filled in from the
        parent ``TelegramConfig`` values so that callers always receive a complete
        configuration object.

        Args:
            name (str): Key into ``self.bots`` identifying the bot to resolve.

        Returns:
            TelegramBotConfig: A new instance with all optional fields populated
                from parent defaults where the bot-specific value was absent.
        """
        bot = self.bots[name]
        return TelegramBotConfig.model_validate({
            "botToken": bot.bot_token,
            "agent": bot.agent,
            "allowedUsers": bot.allowed_users if bot.allowed_users is not None else self.allowed_users,
            "deniedUsers": bot.denied_users if bot.denied_users is not None else self.denied_users,
            "typingIndicator": bot.typing_indicator if bot.typing_indicator is not None else self.typing_indicator,
            "streaming": bot.streaming if bot.streaming is not None else self.streaming,
        })


class DiscordConfig(BaseModel):
    """Discord channel configuration."""
    enabled: bool = False
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    guilds: List[Dict[str, str]] = Field(default_factory=list)

class SlackConfig(BaseModel):
    """Slack channel configuration."""
    enabled: bool = False
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    signing_secret: Optional[str] = Field(default=None, validation_alias="signingSecret")
    allowed_users: List[str] = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: List[str] = Field(default_factory=list, validation_alias="deniedUsers")
    # Reply in thread when message is part of a Slack thread
    threading: bool = True

class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    phone_id: Optional[str] = Field(default=None, validation_alias="phoneId")
    access_token: Optional[str] = Field(default=None, validation_alias="accessToken")
    allowed_users: List[str] = Field(default_factory=list, validation_alias="allowedUsers")
    denied_users: List[str] = Field(default_factory=list, validation_alias="deniedUsers")

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


class ChromeDevtoolsMcpConfig(BaseModel):
    """Chrome DevTools MCP server configuration.

    Uses the official chrome-devtools-mcp package to expose Chrome browser
    control tools (navigation, screenshots, console, network, performance)
    to agents via the Model Context Protocol.

    Requires Node.js. chrome-devtools-mcp is spawned as an MCP stdio server
    by FastAgent when an agent lists "chrome-devtools" in its mcp_servers.

    By default (autoConnect: true), chrome-devtools-mcp launches and manages
    its own isolated Chrome session automatically — no manual Chrome setup needed.

    Example (minimal)::

        browser:
          chromeDevtoolsMcp:
            enabled: true

    To connect to a manually launched Chrome instead::

        browser:
          chromeDevtoolsMcp:
            enabled: true
            autoConnect: false
            browserUrl: "http://127.0.0.1:9222"

    Then add "chrome-devtools" to your agent::

        agents:
          assistant:
            mcpServers: [pyclaw, fetch, chrome-devtools]
    """
    enabled: bool = False
    # Command to run — defaults to globally installed binary; set to "npx" to use npx
    command: str = "chrome-devtools-mcp"
    # Connect to a running Chrome instance with remote debugging enabled.
    # Start Chrome with: --remote-debugging-port=9222
    browser_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("browser_url", "browserUrl"),
    )
    # Auto-connect: launch and manage an isolated Chrome session automatically.
    # When True (default), chrome-devtools-mcp owns the browser lifecycle.
    auto_connect: bool = Field(
        default=True,
        validation_alias=AliasChoices("auto_connect", "autoConnect"),
    )
    # Run Chrome headless (no visible window)
    headless: bool = False
    # Chrome channel: stable | canary | beta | dev
    channel: Optional[str] = None
    # Path to Chrome executable (overrides auto-detection)
    executable_path: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("executable_path", "executablePath"),
    )
    # Expose only 3 basic tools: navigate, evaluate, screenshot
    slim: bool = False


class BrowserAutomationConfig(BaseModel):
    """Browser automation configuration."""
    enabled: bool = False
    headless: bool = True
    slow_mo: int = 0
    timeout: int = 30000
    viewport_width: int = 1280
    viewport_height: int = 720
    chrome_devtools_mcp: ChromeDevtoolsMcpConfig = Field(
        default_factory=ChromeDevtoolsMcpConfig,
        validation_alias=AliasChoices("chrome_devtools_mcp", "chromeDevtoolsMcp"),
    )


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
    # When True, a session whose last activity was before today's midnight
    # (local time) is automatically archived and a fresh session is started.
    daily_rollover: bool = Field(
        default=True,
        validation_alias=AliasChoices("daily_rollover", "dailyRollover"),
    )


class ConcurrencyConfig(BaseModel):
    """Global concurrency fallback.

    Per-model limits are defined under each provider's ``models:`` block.
    This ``default`` applies to any model not explicitly listed there.

    Example:
        concurrency:
          default: 5   # optional — omit to use the built-in default of 3
    """
    default: int = 3


class AcpConfig(BaseModel):
    """FastAgent ACP (Agent Client Protocol) integration settings."""
    enabled: bool = True


class A2AAgentConfig(BaseModel):
    """Per-agent A2A (Agent-to-Agent) protocol configuration."""
    model_config = ConfigDict(populate_by_name=True)
    enabled: bool = True
    allow_inbound: bool = Field(
        default=True,
        validation_alias=AliasChoices("allow_inbound", "allowInbound"),
        description="Accept inbound A2A tasks from external callers.",
    )
    allow_outbound: bool = Field(
        default=False,
        validation_alias=AliasChoices("allow_outbound", "allowOutbound"),
        description="Allow this agent to call other agents via A2A client tools.",
    )
    # "shared" (default) — inbound tasks use the agent's active session, giving
    # the agent full conversation context.  "isolated" — each inbound A2A task
    # gets its own fresh session with no prior context.
    session_mode: str = Field(
        default="shared",
        validation_alias=AliasChoices("session_mode", "sessionMode"),
    )


class GatewayA2AConfig(BaseModel):
    """Gateway-level A2A configuration."""
    model_config = ConfigDict(populate_by_name=True)
    enabled: bool = False  # opt-in; set to true to expose A2A endpoints


class GatewayConfig(BaseModel):
    """Gateway server configuration."""
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_port: int = Field(default=8081, validation_alias=AliasChoices("mcp_port", "mcpPort"))
    debug: bool = False
    log_level: str = "info"
    log_retention_days: int = Field(default=7, validation_alias=AliasChoices("log_retention_days", "logRetentionDays"))
    webhook_url: Optional[str] = Field(default=None, validation_alias="webhookUrl")
    cors_origins: List[str] = Field(default_factory=lambda: ["*"], validation_alias="corsOrigins")
    # Additional skill search directories (on top of ~/.pyclaw/skills/)
    skills_dirs: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("skills_dirs", "skillsDirs"),
    )
    # A2A (Agent-to-Agent) protocol global settings.
    a2a: Optional[GatewayA2AConfig] = Field(
        default=None,
        validation_alias=AliasChoices("a2a"),
    )


@reflect_system("config")
class Config(BaseModel):
    """Root configuration model.

    Loaded from ``~/.pyclaw/config.yaml`` (or ``--config`` path).  All top-level
    keys use camelCase in YAML and are mapped via ``validation_alias`` /
    ``AliasChoices`` to snake_case Pydantic fields.

    Inline secret syntax is resolved by ``ConfigLoader`` before model
    construction: ``${env:VAR}``, ``${keychain:Name}``, ``${file:path}``.

    Section summary:
        providers — LLM provider credentials and model lists
        agents    — per-agent model, MCP servers, tools, vault config
        channels  — Telegram bots, Slack workspace, allowed/denied users
        gateway   — host/port, skills dirs, debug flags, A2A config
        memory    — backend selection (clawvault)
        security  — exec approval mode, audit logging, sandbox
        jobs      — scheduler timezone and agents dir
        sessions  — TTL, daily rollover, max sessions

    See ``reflect(category="config", name=<section>)`` for per-section schema.
    """
    version: str = "1.0"
    # IANA timezone name (e.g. "America/New_York", "Europe/London").
    # All pyclaw timestamps use this zone.  Omit to use the system local timezone.
    timezone: Optional[str] = Field(default=None)
    acp: AcpConfig = Field(default_factory=AcpConfig)
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
