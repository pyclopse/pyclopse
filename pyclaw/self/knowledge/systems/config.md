# Config System

**Files:** `pyclaw/config/schema.py`, `pyclaw/config/loader.py`

---

## Schema Conventions

The config schema is Pydantic v2. YAML uses camelCase keys; Python models use
snake_case with `validation_alias` or `AliasChoices` to bridge them.

**Always test config with `model_validate` and camelCase keys:**
```python
# Correct
config = AgentConfig.model_validate({"contextWindow": 200000})

# Wrong — will silently ignore the field
config = AgentConfig(context_window=200000)
```

Most fields use `AliasChoices` to accept both forms:
```python
mcp_port: int = Field(
    default=8081,
    validation_alias=AliasChoices("mcp_port", "mcpPort"),
)
```

---

## Top-Level Structure

```yaml
version: "1.0"
timezone: America/New_York

gateway:
  host: 0.0.0.0
  port: 8080
  mcpPort: 8081
  selfPort: 8082
  logLevel: info
  logRetentionDays: 7

security:
  execApprovals:
    mode: allowlist    # allowlist | denylist | all | none
    safeBins: [ls, cat, python3]
  sandbox:
    enabled: false
  audit:
    enabled: true
    retentionDays: 30

sessions:
  ttlHours: 24
  reaperIntervalMinutes: 60
  maxSessions: 1000
  sessionTimeout: 3600
  dailyRollover: true

memory:
  backend: file
  embedding:
    enabled: false

jobs:
  enabled: true
  agentsDir: ~/.pyclaw/agents
  defaultTimezone: America/New_York

providers:
  openai:
    apiKey: ${env:OPENAI_API_KEY}
  anthropic:
    apiKey: ${keychain:AnthropicKey}

agents:
  assistant:
    name: Assistant
    model: sonnet
    useFastagent: true
    mcpServers: [pyclaw, self, fetch]
    tools:
      profile: full

channels:
  telegram:
    enabled: true
    botToken: ${env:TELEGRAM_BOT_TOKEN}
    allowedUsers: [123456789]

hooks:
  bundled:
    - session-memory
    - boot-md
```

---

## Inline Secrets

Secret placeholders are resolved at load time before Pydantic validation.

| Syntax | Source |
|--------|--------|
| `${env:VAR_NAME}` | Environment variable |
| `${keychain:Entry Name}` | macOS Keychain (service = `pyclaw`) |
| `${file:~/.secret}` | File contents (trimmed) |
| `${provider:name}` | Named secrets provider |

Resolution happens in `pyclaw/config/loader.py` via `SecretsManager`.

---

## Key Config Classes

| Class | Purpose |
|-------|---------|
| `Config` | Root model |
| `GatewayConfig` | Host, ports, logging |
| `AgentConfig` | Per-agent: model, servers, prompt flags, queue |
| `SecurityConfig` | Approvals, sandbox, audit, per-channel overrides |
| `SessionsConfig` | TTL, reaper interval, rollover |
| `MemoryConfig` | Backend, embedding provider |
| `JobsConfig` | Enabled, agents dir, timezone |
| `TelegramConfig` | Token, allowed/denied users, typing, threading |
| `SlackConfig` | Token, threading, pulse channel |
| `ProvidersConfig` | OpenAI, Anthropic, MiniMax API keys/URLs |
| `HooksConfig` | Bundled and custom hook registrations |

---

## Loading

```python
from pyclaw.config.loader import ConfigLoader

loader = ConfigLoader("~/.pyclaw/config.yaml")
config = loader.load()  # resolves secrets, validates with Pydantic
```

---

## Validation in Tests

```python
from pyclaw.config.schema import ExecApprovalsConfig, AgentConfig

# Always model_validate with camelCase
cfg = ExecApprovalsConfig.model_validate({"mode": "allowlist", "safeBins": ["ls"]})
agent = AgentConfig.model_validate({"name": "Test", "model": "sonnet"})
```
