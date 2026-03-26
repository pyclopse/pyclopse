# pyclopse - Python Gateway

> **⚠️ IMPORTANT: pyclopse is its own independent project.** It is inspired by OpenClaw but is NOT a port, clone, or 1:1 rewrite. pyclopse is designed to be **better**, **cleaner**, and **more secure** with its own architecture, naming conventions, and design philosophy.

## Overview

**pyclopse** is a Python-based gateway *inspired by* OpenClaw, but it is its own independent project—not a port or 1:1 clone. It is NOT an SDK or client library. It is a standalone gateway built with Python and `uv`, designed to be **better**, **cleaner**, and **more secure** than OpenClaw.

Different naming conventions, class names, and architectural patterns are explicitly welcome. pyclopse should reflect Python idioms and best practices rather than mirroring OpenClaw's TypeScript design.

## Goals

- Build a fully functional gateway that improves on OpenClaw's design
- Leverage Python's ecosystem for improved maintainability and developer experience
- Provide a clean API-first architecture for mobile app support
- Exceed OpenClaw's security model with a more rigorous, principled approach
- Maintain clean, readable, and well-organized code as a first-class concern

---

## 1. Project Structure

### 1.1 Python Package Layout

```
pyclopse/
├── pyproject.toml              # uv project configuration
├── uv.lock                     # Locked dependencies
├── pyclopse/                    # Main package
│   ├── __init__.py
│   ├── __main__.py           # CLI entry point
│   ├── config/               # Configuration system
│   │   ├── __init__.py
│   │   ├── schema.py         # YAML schema definitions (Pydantic)
│   │   ├── loader.py         # Config file loading
│   │   ├── validation.py     # Config validation
│   │   └── defaults.py       # Default values
│   ├── core/                 # Core gateway logic
│   │   ├── __init__.py
│   │   ├── gateway.py        # Main gateway class
│   │   ├── agent.py          # Agent management
│   │   ├── session.py        # Session handling
│   │   └── router.py         # Message routing
│   ├── security/             # Security system
│   │   ├── __init__.py
│   │   ├── audit.py          # Security audit
│   │   ├── approvals.py      # Exec approvals
│   │   ├── sandbox.py        # Command sandboxing
│   │   ├── safe_bins.py      # Safe bin policies
│   │   └── audit_logger.py   # Audit logging
│   ├── jobs/                 # Cron system (renamed from "cron")
│   │   ├── __init__.py
│   │   ├── scheduler.py      # Job scheduler
│   │   ├── runner.py         # Job execution
│   │   ├── store.py          # Job persistence
│   │   └── types.py          # Job types
│   ├── pulse/            # Pulse system
│   │   ├── __init__.py
│   │   ├── runner.py         # Pulse runner
│   │   ├── scheduler.py      # Pulse scheduling
│   │   └── triggers.py       # Pulse triggers
│   ├── memory/               # Memory integration
│   │   ├── __init__.py
│   │   ├── client.py         # ClawVault CLI wrapper (subprocess)
│   │   ├── store.py          # Memory storage
│   │   └── queries.py        # Memory queries
│   ├── channels/             # Channel adapters
│   │   ├── __init__.py
│   │   ├── base.py           # Base channel class
│   │   ├── telegram.py       # Telegram adapter
│   │   ├── discord.py        # Discord adapter
│   │   ├── slack.py          # Slack adapter
│   │   ├── whatsapp.py       # WhatsApp adapter
│   │   ├── signal.py         # Signal adapter
│   │   ├── imessage.py       # iMessage adapter
│   │   └── registry.py       # Channel registry
│   ├── providers/            # Model providers
│   │   ├── __init__.py
│   │   ├── base.py           # Base provider class
│   │   ├── openai.py         # OpenAI provider
│   │   ├── anthropic.py      # Anthropic provider
│   │   ├── google.py         # Google provider
│   │   ├── fastagent.py      # FastAgent integration
│   │   └── registry.py       # Provider registry
│   ├── workflows/            # Workflow engine (FastAgent)
│   │   ├── __init__.py
│   │   ├── runner.py         # Workflow execution
│   │   ├── patterns.py       # Built-in patterns
│   │   ├── mcp_integration.py # MCP tool registry
│   │   └── config.py         # Workflow YAML loading
│   ├── plugins/              # Plugin system
│   │   ├── __init__.py
│   │   ├── loader.py         # Plugin loader
│   │   ├── registry.py       # Plugin registry
│   │   ├── hooks.py          # Hook system
│   │   └── http.py           # Plugin HTTP endpoints
│   ├── tui/                  # Terminal UI
│   │   ├── __init__.py
│   │   ├── app.py            # TUI application
│   │   ├── chat.py           # Chat view
│   │   ├── sessions.py       # Session management view
│   │   └── components/       # UI components
│   ├── api/                  # REST API (for mobile)
│   │   ├── __init__.py
│   │   ├── app.py            # FastAPI application
│   │   ├── routes/           # API routes
│   │   │   ├── sessions.py
│   │   │   ├── agents.py
│   │   │   ├── channels.py
│   │   │   └── memory.py
│   │   └── middleware.py     # API middleware
│   └── utils/                # Utilities
│       ├── __init__.py
│       ├── logging.py        # Logging utilities
│       ├── http.py           # HTTP utilities
│       └── crypto.py         # Cryptography
├── tests/                    # Test suite
├── docs/                     # Documentation
└── scripts/                  # Utility scripts
```

### 1.2 Agent Directory Structure

All agents (including main) are stored in the `agents/` directory:

```
pyclopse/
├── agents/
│   ├── main/                    # Main agent (NOT in workspace/)
│   │   ├── SOUL.md              # Agent personality
│   │   ├── RULES.md             # Operational rules
│   │   ├── PULSE.md             # Pulse/heartbeat config
│   │   ├── AGENTS.md            # Shared agent config
│   │   ├── MEMORY.md            # Long-term memory
│   │   └── memory/              # Agent-specific memory
│   │       └── YYYY-MM-DD.md    # Daily memory logs
│   ├── agent-1/                 # Sub-agent 1
│   │   ├── SOUL.md
│   │   ├── RULES.md
│   │   └── memory/
│   └── agent-2/                 # Sub-agent 2
│       ├── SOUL.md
│       ├── RULES.md
│       └── memory/
```

**Key difference from OpenClaw:**
- OpenClaw: Main agent in `workspace/`, subagents in `agents/`
- pyclopse: All agents in `agents/` directory

---

## 1.3 OpenClaw Migration Compatibility

### File Name Aliases

The gateway supports both pyclopse naming conventions AND OpenClaw naming conventions:

| pyclopse Name | OpenClaw Alias | Purpose |
|-------------|-----------------|---------|
| PULSE.md | HEARTBEAT.md | Pulse/heartbeat config |
| AGENTS.md | (same) | Shared agent config |
| MEMORY.md | MEMORY.md | Long-term memory |
| SOUL.md | SOUL.md | Agent personality |
| RULES.md | RULES.md | Operational rules |

### Compatibility Layer Behavior

The gateway loads agent files as follows:

1. **Check for pyclopse name first** (e.g., `PULSE.md`)
2. **Fall back to OpenClaw name if not found** (e.g., `HEARTBEAT.md`)

This allows easy migration:
- Copy files from OpenClaw's `~/.openclaw/workspace/` to pyclopse's `agents/main/` should just work
- Existing OpenClaw users can migrate by simply copying their agent files
- No renaming required - the compatibility layer handles it

### Migration Example

```bash
# OpenClaw files (location)
~/.openclaw/workspace/SOUL.md
~/.openclaw/workspace/RULES.md
~/.openclaw/workspace/HEARTBEAT.md

# Copy to pyclopse (just works!)
cp -r ~/.openclaw/workspace/* ~/.pyclopse/agents/main/

# pyclopse automatically maps:
# - HEARTBEAT.md → PULSE.md
# - SOUL.md → SOUL.md (same name)
# - RULES.md → RULES.md (same name)
```

---

```toml
[project]
name = "pyclopse"
version = "0.1.0"
description = "Python gateway - rewrite of OpenClaw"
requires-python = ">=3.11"
dependencies = [
    "uv>=0.1.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "pyyaml>=6.0.0",
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0",
    "httpx>=0.24.0",
    "python-dotenv>=1.0.0",
    "rich>=13.0.0",
    "textual>=0.1.0",
    "aiosqlite>=0.19.0",
    "jwt>=1.3.1",
    "python-jose>=3.3.0",
    "passlib>=1.7.4",
    "cryptography>=41.0.0",
    "fast-agent-mcp>=0.1.0",  # FastAgent workflow engine
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.0.0",
    "ruff>=0.0.280",
    "mypy>=1.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## 2. Core Architecture

### 2.1 Gateway Boot Process

1. **Load Configuration**: Read YAML config file, validate with Pydantic
2. **Initialize Security**: Set up audit logging, exec approvals, sandbox policies
3. **Initialize Memory**: Connect to ClawVault via CLI subprocess
4. **Initialize Providers**: Set up model providers (FastAgent or custom)
5. **Initialize Channels**: Load channel adapters
6. **Initialize Plugins**: Load and enable plugins
7. **Initialize Jobs**: Start job scheduler
8. **Initialize Pulse**: Start pulse runner
9. **Initialize TUI**: Start terminal UI (if not headless)
10. **Initialize API**: Start REST API server (for mobile support)

### 2.2 Message Flow

```
Channel -> Router -> Agent -> Provider -> Response -> Channel
                     |
                     v
                   Memory (store/retrieve)
```

### 2.3 Key Classes

These are conceptual roles—naming is not required to match OpenClaw. Use Python conventions and whatever names best express intent.

**Gateway** (or `GatewayServer`, `ClawGateway`, etc.): Main entry point, orchestrates all subsystems

**Agent** (or `AgentRunner`, `ConversationAgent`, etc.): Handles conversation state, tool execution, provider interaction

**Session** (or `Conversation`, `ConversationSession`, etc.): Represents a conversation session with context

**Router** (or `MessageRouter`, `Dispatcher`, etc.): Routes incoming messages to appropriate agents/sessions

**ChannelAdapter** (or `Channel`, `ChannelBackend`, etc.): Abstracts different chat platforms

---

## Concurrency Strategy

> ⚠️ **This section is critical for future implementers.** It describes a key architectural mistake in OpenClaw that pyclopse intentionally avoids.

### The OpenClaw Problem

OpenClaw uses a **nested lane architecture** with **double-locking** that causes severe concurrency issues:

- **Nested lane architecture**: Session lane (maxConcurrent: 1) sits inside a global lane
- **Double-locking**: Before any agent can run, it must acquire both the session lock AND the global lock
- **Blocking cascade**: The session lane with `maxConcurrent: 1` blocks BEFORE checking the global lane
- **Total blockage**: One blocked agent blocks ALL agents — true concurrency is impossible

This design means:
- If one agent is busy, every other agent waits
- Long-running sessions prevent any new sessions from starting
- The system cannot scale beyond a few concurrent users

### pyclopse's Solution

pyclopse uses a **true parallel execution model** based on Python's asyncio:

- **Per-agent async tasks**: Each agent/session gets its own independent async task
- **No global lock**: Sessions run truly in parallel — no shared lock between different agents
- **Isolated state**: Each session maintains its own state without interfering with others
- **Per-session locking only**: Locking is only needed within the same session (if at all), not across sessions

```python
# pyclopse approach - each session runs as its own async task
async def handle_session(session_id: str, messages: list):
    """Each session runs independently in its own task"""
    agent = Agent(session_id=session_id)
    await agent.run(messages)

async def gateway_main():
    # Spawn each session as a separate async task - they run in parallel!
    tasks = [
        asyncio.create_task(handle_session(sid, msgs))
        for sid, msgs in incoming_sessions
    ]
    await asyncio.gather(*tasks)  # True parallel execution
```

**Key principles:**
1. **Never block other agents** — use async/await for I/O-bound work
2. **No shared global lock** — each session is independent
3. **Use asyncio primitives** — `asyncio.create_task()`, `asyncio.gather()`, etc.
4. **Isolate session state** — no shared mutable state between sessions

This ensures pyclopse can handle many concurrent users without one blocking all others.

---

## 3. Configuration System

### 3.1 YAML Configuration Format

Configuration is stored in `~/.pyclopse/config.yaml`:

```yaml
# pyclopse configuration
version: "1.0"

# Gateway settings
gateway:
  host: "0.0.0.0"
  port: 8080
  debug: false
  logLevel: "info"

# Security settings
security:
  execApprovals:
    mode: "allowlist"  # allowlist, denylist, all, none
    safeBins:
      - "/bin/ls"
      - "/bin/cat"
      - "/usr/bin/git"
    alwaysApprove:
      - "git status"
      - "ls *"
  sandbox:
    enabled: true
    type: "docker"  # docker, none
    docker:
      image: "pyclopse-sandbox:latest"
      network: "none"
  audit:
    enabled: true
    logFile: "~/.pyclopse/logs/audit.log"
    retentionDays: 90

# Memory (ClawVault)
memory:
  backend: "clawvault"
  clawvault:
    vault_path: "~/.claw/vault"

# Providers (model configuration)
providers:
  openai:
    enabled: true
    apiKey: "${OPENAI_API_KEY}"
    defaultModel: "gpt-4"
  anthropic:
    enabled: true
    apiKey: "${ANTHROPIC_API_KEY}"
    defaultModel: "claude-3-opus-20240229"
  fastagent:
    enabled: true
    url: "http://localhost:8000"
    defaultModel: "anthropic/claude-3-5-sonnet"

# Workflows (FastAgent)
workflows:
  configPath: "~/.pyclopse/workflows.yaml"
  enabled: true
  defaultWorkflow: "research_and_write"

# Agents (FastAgent-First)
# 
# pyclopse uses FastAgent as the backbone. YAML config compiles to @fast.agent decorators,
# OR you can load Python files that define agents directly with decorators.

agents:
  default:
    name: "Assistant"
    model: "openai/gpt-4"
    maxTokens: 4096
    temperature: 0.7
    systemPrompt: "You are a helpful assistant."
    tools:
      enabled: true
      allowlist:
        - "bash"
        - "read"
        - "write"
        - "web_search"
    pulse:
      enabled: true
      every: "30m"
      prompt: "Check for any important updates."
      activeHours:
        start: "08:00"
        end: "22:00"

  # Single agent - YAML compiles to @fast.agent decorator
  assistant:
    instruction: "You are helpful..."
    model: sonnet
    # No workflow = single agent (default)
    
  # Chain workflow - sequential steps
  reporter:
    instruction: "Create reports..."
    workflow: chain
    agents: [fetcher, analyzer, writer]

  # Parallel workflow - fan-out to multiple sub-agents
  researcher:
    instruction: "Research things..."
    workflow: parallel
    agents: [web_searcher, url_fetcher]
    
  # Orchestrator-workers pattern
  pmo:
    instruction: "Manage projects across regions"
    workflow: agents_as_tools
    agents: [ny_manager, london_manager]

# Jobs (cron)
jobs:
  enabled: true
  persistFile: "~/.pyclopse/jobs.json"

# Channels
channels:
  telegram:
    enabled: true
    botToken: "${TELEGRAM_BOT_TOKEN}"
    allowedUsers:
      - 8327082847
  discord:
    enabled: false
    botToken: "${DISCORD_BOT_TOKEN}"
    guilds:
      - id: "123456789"
  slack:
    enabled: false
    botToken: "${SLACK_BOT_TOKEN}"
    signingSecret: "${SLACK_SIGNING_SECRET}"
  whatsapp:
    enabled: false
    phoneId: "${WHATSAPP_PHONE_ID}"
    accessToken: "${WHATSAPP_ACCESS_TOKEN}"

# Plugins
plugins:
  enabled: true
  autoEnable: true
  entries:
    # Python native plugin
    my-plugin:
      type: python
      path: ./plugins/my_plugin.py
      config:
        option1: "value1"
    
    # HTTP/RPC plugin (any language!)
    telegram-bot:
      type: http
      url: http://localhost:9001/webhook
      health: http://localhost:9001/health
      config:
        token: "${TELEGRAM_BOT_TOKEN}"
    
    # Subprocess plugin (stdio communication)
    custom_shell:
      type: subprocess
      command: ./my_plugin.sh
      protocol: json  # communicate via JSON stdin/stdout
    
    # JSON config-only plugin
    simple_webhook:
      type: json
      config:
        route: /webhook
        response: "Hello"

### Plugin Types

pyclopse supports multiple plugin types:

| Type | Description | Use Case |
|------|-------------|----------|
| `python` | Native Python plugin (loaded directly) | Maximum flexibility, full API access |
| `http` | HTTP/RPC plugin (separate process) | Any language (Go, Rust, Node, etc.) |
| `subprocess` | stdio communication (any language) | Simple shell scripts, binaries |
| `json` | Config-only plugins (no code) | Simple webhooks, static responses |

#### Python Plugins

```yaml
my-plugin:
  type: python
  path: ./plugins/my_plugin.py  # or module: "mymodule.plugin"
  config:
    option1: "value1"
```

Python plugins define a `Plugin` or `ChannelPlugin` subclass.

#### HTTP Plugins

```yaml
telegram-bot:
  type: http
  url: http://localhost:9001/webhook
  health: http://localhost:9001/health
```

HTTP plugins run as separate processes and communicate via HTTP. Any language can be used.

#### Subprocess Plugins

```yaml
custom_shell:
  type: subprocess
  command: ./my_plugin.sh
  protocol: json  # or "text"
```

Subprocess plugins communicate via stdin/stdout. Use `protocol: json` for JSON messages.

#### JSON Plugins

```yaml
simple_webhook:
  type: json
  config:
    route: /webhook
    response: "Hello"
```

JSON plugins are config-only with no executable code.

# Hooks
hooks:
  internal:
    enabled: true
  external:
    enabled: true
  entries:
    gmail-watcher:
      enabled: false

# TUI
tui:
  enabled: true
  theme: "dark"
  keyBindings:
    ctrlC: "exit"
    ctrlZ: "suspend"

# Memory QMD
memoryQmd:
  enabled: false
  paths:
    - path: "~/memory"
      name: "Long-term"
```

### 3.2 Pydantic Models

```python
# pyclopse/config/schema.py
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum

class SecurityMode(str, Enum):
    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"
    ALL = "all"
    NONE = "none"

class ExecApprovalsConfig(BaseModel):
    mode: SecurityMode = SecurityMode.ALLOWLIST
    safeBins: List[str] = Field(default_factory=list)
    alwaysApprove: List[str] = Field(default_factory=list)

class SandboxConfig(BaseModel):
    enabled: bool = True
    type: str = "none"  # docker, none
    docker: Optional[Dict[str, Any]] = None

class AuditConfig(BaseModel):
    enabled: bool = True
    logFile: str = "~/.pyclopse/logs/audit.log"
    retentionDays: int = 90

class SecurityConfig(BaseModel):
    execApprovals: ExecApprovalsConfig = Field(default_factory=ExecApprovalsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

class ClawVaultConfig(BaseModel):
    """ClawVault CLI wrapper config - not HTTP, it's an npm package"""
    vault_path: str = "~/.claw/vault"  # Path to vault directory
    enabled: bool = True

class MemoryConfig(BaseModel):
    backend: str = "clawvault"
    clawvault: ClawVaultConfig = Field(default_factory=ClawVaultConfig)

class ProviderConfig(BaseModel):
    enabled: bool = True
    apiKey: Optional[str] = None
    defaultModel: Optional[str] = None

class ProvidersConfig(BaseModel):
    openai: Optional[ProviderConfig] = None
    anthropic: Optional[ProviderConfig] = None
    google: Optional[ProviderConfig] = None
    fastagent: Optional[ProviderConfig] = None

# ... more models
```

---

## 4. Security Model

### 4.1 Exec Approvals

- **Allowlist Mode**: Only commands in `safeBins` can run
- **Denylist Mode**: All commands except those in `safeBins` can run
- **All Mode**: All commands approved (dangerous!)
- **None Mode**: No commands approved

```python
# pyclopse/security/approvals.py
from dataclasses import dataclass
from typing import List, Set, Optional
import re

@dataclass
class ApprovalRequest:
    command: str
    args: List[str]
    cwd: str
    agent_id: str
    session_id: str

class ExecApprovalSystem:
    def __init__(self, config):
        self.mode = config.mode
        self.safe_bins = set(config.safe_bins)
        self.always_approve = [re.compile(p) for p in config.always_approve]
    
    async def should_approve(self, request: ApprovalRequest) -> bool:
        # Check always_approve patterns first
        for pattern in self.always_approve:
            if pattern.search(request.command):
                return True
        
        # Check safe bins
        if self.mode == "allowlist":
            return self._is_safe_bin(request.command)
        elif self.mode == "denylist":
            return not self._is_safe_bin(request.command)
        elif self.mode == "all":
            return True
        return False
    
    def _is_safe_bin(self, command: str) -> bool:
        cmd_name = command.split()[0] if command else ""
        return cmd_name in self.safe_bins
```

### 4.2 Sandboxing

- **Docker Sandboxing**: Run commands in isolated containers
- **None**: No sandboxing (development only)

```python
# pyclopse/security/sandbox.py
import asyncio
from typing import Optional, Dict, Any

class Sandbox:
    async def execute(self, command: str, cwd: str, env: Dict[str, str]) -> Any:
        raise NotImplementedError

class DockerSandbox(Sandbox):
    def __init__(self, image: str = "pyclopse-sandbox:latest"):
        self.image = image
    
    async def execute(self, command: str, cwd: str, env: Dict[str, str]):
        # Use docker run --rm -v ... command
        # Implementation similar to OpenClaw's docker.ts
        pass
```

### 4.3 Audit Logging

```python
# pyclopse/security/audit_logger.py
import json
import logging
from datetime import datetime
from pathlib import Path

class AuditLogger:
    def __init__(self, log_file: str, retention_days: int = 90):
        self.log_file = Path(log_file).expanduser()
        self.retention_days = retention_days
        self.logger = logging.getLogger("pyclopse.audit")
    
    async def log(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": event_type,
            "data": data
        }
        self.logger.info(json.dumps(entry))
    
    async def run_audit(self) -> dict:
        # Port security checks from OpenClaw
        findings = []
        # ... implement audit checks
        return {"findings": findings, "summary": {...}}
```

---

## 5. Channel Adapters

### 5.1 Base Adapter Pattern

```python
# pyclopse/channels/base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass

@dataclass
class Message:
    id: str
    channel: str
    sender: str
    content: str
    timestamp: datetime
    metadata: Dict[str, Any]

@dataclass
class MessageTarget:
    channel: str
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    thread_id: Optional[str] = None

class ChannelAdapter(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    @abstractmethod
    async def connect(self):
        """Establish connection to the channel"""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """Close connection"""
        pass
    
    @abstractmethod
    async def send_message(self, target: MessageTarget, content: str):
        """Send a message to the channel"""
        pass
    
    @abstractmethod
    async def start_listening(self, handler: callable):
        """Start listening for incoming messages"""
        pass
    
    @abstractmethod
    async def react(self, message_id: str, emoji: str):
        """Add reaction to a message"""
        pass
```

### 5.2 Channel Registry

```python
# pyclopse/channels/registry.py
from typing import Dict, Type
from .base import ChannelAdapter

CHANNEL_REGISTRY: Dict[str, Type[ChannelAdapter]] = {}

def register_channel(name: str):
    def decorator(cls: Type[ChannelAdapter]):
        CHANNEL_REGISTRY[name] = cls
        return cls
    return decorator

def get_channel(name: str, config: dict) -> ChannelAdapter:
    if name not in CHANNEL_REGISTRY:
        raise ValueError(f"Unknown channel: {name}")
    return CHANNEL_REGISTRY[name](config)
```

### 5.3 Telegram Adapter Example

```python
# pyclopse/channels/telegram.py
from .base import ChannelAdapter, Message, MessageTarget
from telegram import Bot
from telegram.error import TelegramError

@register_channel("telegram")
class TelegramAdapter(ChannelAdapter):
    def __init__(self, config: dict):
        super().__init__(config)
        self.bot = None
        self.token = config.get("bot_token")
    
    async def connect(self):
        self.bot = Bot(token=self.token)
    
    async def send_message(self, target: MessageTarget, content: str):
        await self.bot.send_message(
            chat_id=target.user_id,
            text=content
        )
    
    async def start_listening(self, handler: callable):
        # Use webhook or long polling
        pass
    
    async def react(self, message_id: str, emoji: str):
        pass
```

---

## 6. Memory Integration (ClawVault)

### 6.1 CLI Wrapper (Subprocess)

ClawVault is an npm package (`npm install -g clawvault`) that runs via CLI. We wrap it with subprocess:

```python
# pyclopse/memory/client.py
import subprocess
import json
import asyncio
from typing import Optional, List, Dict, Any

class ClawVaultClient:
    """Wrapper around clawvault CLI (not HTTP - it's an npm package)"""
    
    def __init__(self, vault_path: str = "~/.claw/vault"):
        self.vault_path = vault_path
    
    async def observe(self, session_path: str, compress: bool = True) -> List[dict]:
        """Run clawvault observe --compress on session file"""
        cmd = ["clawvault", "observe", "--compress", session_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # Parse observation output...
        return parsed_observations
    
    async def search(self, query: str, limit: int = 10) -> List[dict]:
        """Run clawvault vsearch"""
        cmd = ["clawvault", "vsearch", query, "--limit", str(limit)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return parsed_results
    
    async def wake(self) -> dict:
        """Run clawvault wake to restore context"""
        result = subprocess.run(["clawvault", "wake"], capture_output=True, text=True)
        return parse_wake_output(result.stdout)
    
    async def checkpoint(self, session_path: str) -> str:
        """Run clawvault checkpoint"""
        result = subprocess.run(
            ["clawvault", "checkpoint", session_path],
            capture_output=True, text=True
        )
        return result.stdout.strip()
    
    async def graph(self) -> dict:
        """Get memory graph"""
        result = subprocess.run(["clawvault", "graph"], capture_output=True, text=True)
        return json.loads(result.stdout)
```

### 6.2 Memory Integration

```python
# pyclopse/memory/store.py
from .client import ClawVaultClient

class MemoryStore:
    def __init__(self, config: dict):
        self.client = ClawVaultClient(
            vault_path=config["clawvault"]["vault_path"]
        )
    
    async def store_session_context(self, session_id: str, messages: list):
        await self.client.store({
            "type": "session_context",
            "session_id": session_id,
            "messages": messages
        })
    
    async def recall_relevant(self, query: str, session_id: str) -> list:
        results = await self.client.search(query)
        # Filter to relevant session
        return [r for r in results if r.get("session_id") == session_id]
```

---

## 7. Agents (FastAgent-First)

### 7.1 Agent Definition: YAML Config → FastAgent Decorators

> **Key Insight: Agents Can Chain to Other Agents**
>
> Every pyclopse agent can use other agents as building blocks:
>
> ```yaml
> agents:
>   # Base agents (single FastAgent)
>   order_agent:
>     instruction: "Place orders..."
>     model: sonnet
>     
>   ship_agent:
>     instruction: "Ship orders..."
>     model: sonnet
>     
>   # Chained agent - runs order then ship
>   order_and_ship:
>     workflow: chain
>     agents: [order_agent, ship_agent]
>     
>   # Parallel agent - fans out to multiple
>   research_team:
>     workflow: parallel
>     agents: [web_searcher, data_fetcher]
>     
>   # Agents-as-tools - orchestrator pattern
>   coordinator:
>     workflow: agents_as_tools
>     agents: [order_agent, ship_agent, inventory_agent]
> ```

FastAgent uses **decorators** to define agents, NOT just config. pyclopse supports two approaches:

**Option A: YAML config that compiles to FastAgent decorators**
```yaml
# pyclopse.yaml - compiles to @fast.agent decorators
agents:
  # Base agents (single FastAgent)
  url_fetcher:
    instruction: "Given a URL, provide a summary"
    servers: [fetch]  # MCP server names
    model: sonnet
    
  social_media:
    instruction: "Write a social media post"
    model: sonnet
    
  # Chained agent - runs agents sequentially
  post_writer:
    workflow: chain
    agents: [url_fetcher, social_media]
    
  # Parallel agent - fans out to multiple agents
  researcher:
    workflow: parallel
    agents: [web_searcher, url_fetcher]
    
  # Agents-as-tools - orchestrator calls others as tools
  pmo:
    workflow: agents_as_tools
    agents: [ny_manager, london_manager]
```

**Option B: Load Python files that define agents directly**

```python
# agents/assistant.py - direct FastAgent definition
from fast_agent import FastAgent

fast = FastAgent("pyclopse")

@fast.agent(
    name="assistant",
    instruction="You are a helpful AI assistant.",
    servers=["fetch", "time"],  # MCP tools
    human_input=False,
)
async def main():
    async with fast.run() as agent:
        await agent.interactive()
```

The key insight: **agents are defined with decorators in FastAgent**. pyclopse's YAML config is a convenience layer that compiles to these decorators at runtime.

### 7.2 FastAgent is the Backbone

Every pyclopse "Agent" IS a FastAgent instance. This is the core insight:

```
pyclopse Agent = FastAgent instance + Channel binding + Security layer
```

**What pyclopse adds to FastAgent:**
- Channel integrations (Telegram, Discord, etc.)
- Security layer (exec approvals, audit)
- CLI and TUI
- Persistence (sessions, history)
- ClawVault memory integration

**What FastAgent provides:**
- Multi-provider support (Anthropic, OpenAI, Google, Ollama, etc.)
- All workflow patterns (chain, parallel, maker, agents-as-tools)
- Tool calling (MCP-native)
- Structured outputs, vision, PDF support

### 7.2 Base Provider

```python
# pyclopse/providers/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, AsyncIterator
from dataclasses import dataclass

@dataclass
class Message:
    role: str  # system, user, assistant
    content: str
    tool_calls: Optional[List[Dict]] = None

@dataclass
class ToolResult:
    tool_name: str
    result: str
    is_error: bool = False

class Provider(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_key = config.get("api_key")
    
    @abstractmethod
    async def chat(
        self,
        messages: List[Message],
        model: str,
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """Stream chat completion"""
        pass
    
    @abstractmethod
    async def embed(self, text: str, model: str) -> List[float]:
        """Get embeddings"""
        pass
    
    @property
    @abstractmethod
    def supports_streaming(self) -> bool:
        pass
    
    @property
    @abstractmethod
    def supports_tools(self) -> bool:
        pass
```

### 7.3 FastAgent Integration

We will use [FastAgent](https://github.com/evalstate/fast-agent) as the workflow engine instead of building our own agent system. This provides powerful workflow patterns out of the box:

**Workflow Patterns Supported:**
1. **Chain** - Sequential execution (Agent A → Agent B → Agent C)
2. **Parallel** - Fan-out to multiple agents, fan-in results
3. **Maker** - K-voting error reduction
4. **Agents as Tools** - Routing, parallelization, orchestrator-workers

**Benefits:**
- Proven, well-tested workflow patterns
- MCP-native support
- Multi-provider (Anthropic, OpenAI, Google, Ollama, etc.)
- Built-in structured outputs, vision, PDF support
- Active development

**Implementation:**
- Add fast-agent-mcp to dependencies
- Create pyclopse/workflows/ module that wraps FastAgent
- Define workflows in YAML config
- Agents become FastAgent agents with MCP tools

**Example pyclopse workflow config:**
```yaml
workflows:
  research_and_write:
    type: chain
    agents:
      - research_agent  # fetches data
      - writer_agent    # writes response
```

### 7.4 FastAgent Detailed Integration

This section provides comprehensive details on integrating FastAgent with pyclopse.

#### 7.4.1 Agent Definition Syntax

FastAgent uses the `@fast.agent` decorator to define agents:

```python
# pyclopse/agents/definitions.py
import asyncio
from fast_agent import FastAgent

# Create FastAgent application
fast = FastAgent("pyclopse")

@fast.agent(
    name="assistant",
    instruction="You are a helpful AI assistant.",
    human_input=False,  # Enable for human-in-the-loop
)
async def main():
    async with fast.run() as agent:
        await agent.interactive()
```

#### 7.4.2 Running Agents

```python
# Basic prompt execution
async with fast.run() as agent:
    result = await agent("Your prompt here")
    
# Using .send() method
async with fast.run() as agent:
    await agent.send("message")
```

#### 7.4.3 Workflow Patterns

**Chain (Sequential Execution):**
```python
@fast.agent(
    name="researcher",
    instruction="Research the given topic and provide key findings.",
    servers=["fetch"],  # MCP servers
)
@fast.agent(
    name="writer",
    instruction="Write a summary based on the research.",
)
@fast.chain(
    name="research_and_write",
    sequence=["researcher", "writer"],
)
async def main():
    async with fast.run() as agent:
        await agent.research_and_write("AI trends")
```

**Parallel (Fan-out/Fan-in):**
```python
@fast.agent("translate_fr", "Translate to French")
@fast.agent("translate_de", "Translate to German")
@fast.agent("translate_es", "Translate to Spanish")

@fast.parallel(
    name="translate",
    fan_out=["translate_fr", "translate_de", "translate_es"],
    # Optional: fan_in="aggregator_agent"
)

async def main():
    async with fast.run() as agent:
        result = await agent.translate("Hello world")
```

**Maker (K-Voting Error Reduction):**
```python
@fast.agent(
    name="classifier",
    instruction="Reply with only: A, B, or C.",
)
@fast.maker(
    name="reliable_classifier",
    worker="classifier",
    k=3,  # Number of votes
    max_samples=25,
    match_strategy="normalized",
)
async def main():
    async with fast.run() as agent:
        result = await agent.reliable_classifier("Classify this")
```

**Agents as Tools (Orchestrator-Workers):**
```python
@fast.agent(
    name="NY-Manager",
    instruction="Return NY time and project status.",
    servers=["time"],
)
@fast.agent(
    name="London-Manager", 
    instruction="Return London time and news.",
    servers=["time"],
)
@fast.agent(
    name="PMO-Orchestrator",
    instruction=(
        "Get reports. Always use one tool call per project/news. "
        "NY projects: [OpenAI, Fast-Agent]. London news: [Economics, Art]. "
        "Aggregate results with summary."
    ),
    default=True,  # Default agent for direct prompts
    agents=["NY-Manager", "London-Manager"],  # Expose as tools
)
async def main():
    async with fast.run() as agent:
        await agent("Get PMO report")
```

#### 7.4.4 MCP Integration

FastAgent MCP server configuration is built programmatically by `AgentRunner._build_fa_settings()` from the pyclopse config — no `fastagent.config.yaml` file is used or needed.

#### 7.4.5 YAML Configuration for pyclopse Agents

```yaml
# pyclopse.yaml - compiles to @fast.agent decorators
agents:
  main:
    type: fastagent
    name: assistant
    instruction: "You are a helpful AI assistant."
    model: sonnet
    temperature: 0.7
    max_tokens: 4096
    channels: [telegram, discord]
    servers:
      - fetch
      - time
    
  researcher:
    workflow: chain
    sequence:
      - web_searcher
      - url_fetcher
      - summarizer
    
  translator:
    workflow: parallel
    fan_out: [translate_en, translate_fr, translate_de]
    
  classifier:
    workflow: maker
    worker: basic_classifier
    k: 5
    max_samples: 50

  pmo:
    workflow: agents_as_tools
    agents: [ny_manager, london_manager]

workflows:
  default_agent: main
```

#### 7.4.6 pyclopse Agent Factory

```python
# pyclopse/agents/factory.py
from fast_agent import FastAgent
from typing import Dict, Any, Optional, List

class FastAgentFactory:
    """Factory for creating FastAgent instances from config."""
    
    def __init__(self):
        self._app = None
    
    def create_agent(
        self,
        name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
        human_input: bool = False,
    ) -> FastAgent:
        """Create a FastAgent from configuration."""
        fast = FastAgent(name)
        
        @fast.agent(
            name=name,
            instruction=instruction,
            human_input=human_input,
            servers=servers or [],
        )
        async def agent_func():
            async with fast.run() as agent:
                await agent.interactive()
        
        return fast
    
    def create_workflow(
        self,
        workflow_type: str,
        name: str,
        agents: List[str],
        **kwargs,
    ) -> Any:
        """Create a workflow (chain, parallel, maker, agents_as_tools)."""
        if workflow_type == "chain":
            return self._create_chain(name, agents)
        elif workflow_type == "parallel":
            return self._create_parallel(name, agents, **kwargs)
        elif workflow_type == "maker":
            return self._create_maker(name, agents, **kwargs)
        elif workflow_type == "agents_as_tools":
            return self._create_agents_as_tools(name, agents, **kwargs)
        else:
            raise ValueError(f"Unknown workflow type: {workflow_type}")
```

#### FastAgent Provider Implementation

```python
# pyclopse/providers/fastagent.py
from .base import Provider
from typing import List, Dict, Any, Optional, AsyncIterator
import httpx

class FastAgentProvider(Provider):
    """Wrapper around FastAgent for multi-provider support and workflow patterns"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.url = config.get("url", "http://localhost:8000")
        self.default_model = config.get("default_model", "anthropic/claude-3-5-sonnet")
    
    async def chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """Stream chat completion via FastAgent"""
        model = model or self.default_model
        
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "tools": tools,
                    "stream": True,
                    **kwargs
                }
            ) as response:
                async for chunk in response.aiter_lines():
                    if chunk.startswith("data: "):
                        data = chunk[6:]
                        if data == "[DONE]":
                            break
                        # Parse and yield content
                        yield parse_chunk(data)
    
    async def embed(self, text: str, model: str) -> List[float]:
        """Get embeddings via FastAgent"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.url}/v1/embeddings",
                json={"model": model, "input": text}
            )
            return resp.json()["data"][0]["embedding"]
    
    @property
    def supports_streaming(self) -> bool:
        return True
    
    @property
    def supports_tools(self) -> bool:
        return True
```

#### Workflow Runner

```python
# pyclopse/workflows/runner.py
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum

class WorkflowType(str, Enum):
    CHAIN = "chain"           # Sequential: A → B → C
    PARALLEL = "parallel"    # Fan-out/in: A → [B, C] → D
    MAKER = "maker"          # K-voting for error reduction
    AGENTS_AS_TOOLS = "agents_as_tools"  # Routing pattern

@dataclass
class WorkflowStep:
    agent_id: str
    tools: Optional[List[str]] = None
    input_mapping: Optional[Dict[str, str]] = None  # {"step_output": "next_input"}
    output_key: Optional[str] = None  # Where to store this step's output

@dataclass
class Workflow:
    name: str
    type: WorkflowType
    agents: List[str]  # Agent IDs to use
    steps: Optional[List[WorkflowStep]] = None
    max_workers: Optional[int] = None  # For parallel
    k_votes: Optional[int] = None  # For maker

class WorkflowRunner:
    """Executes workflows using FastAgent patterns"""
    
    def __init__(self, agent_registry, mcp_registry):
        self.agents = agent_registry
        self.mcp = mcp_registry
    
    async def run_chain(self, workflow: Workflow, initial_input: Any) -> Any:
        """Run sequential chain: A → B → C"""
        context = {"input": initial_input}
        
        for step in workflow.steps:
            agent = self.agents.get(step.agent_id)
            # Map inputs from previous step
            input_data = self._map_inputs(context, step.input_mapping)
            # Execute agent
            result = await agent.execute(input_data)
            # Store output
            if step.output_key:
                context[step.output_key] = result
            context["last_output"] = result
        
        return context.get("last_output")
    
    async def run_parallel(self, workflow: Workflow, initial_input: Any) -> Any:
        """Run parallel: A → [B, C, D] → combine results"""
        # Fan-out
        tasks = []
        for step in workflow.steps:
            agent = self.agents.get(step.agent_id)
            input_data = self._map_inputs({"input": initial_input}, step.input_mapping)
            tasks.append(agent.execute(input_data))
        
        # Fan-in: gather results
        import asyncio
        results = await asyncio.gather(*tasks)
        
        # Combine results
        return self._combine_results(results)
    
    async def run_maker(self, workflow: Workflow, input_data: Any) -> Any:
        """Run K-voting maker pattern for error reduction"""
        import asyncio
        
        k = workflow.k_votes or 3
        tasks = []
        
        for _ in range(k):
            # Each agent gets same input, produces independent output
            agent = self.agents.get(workflow.agents[0])
            tasks.append(agent.execute(input_data))
        
        results = await asyncio.gather(*tasks)
        
        # Vote/aggregate results (majority wins, or best of N)
        return self._aggregate_votes(results)
    
    async def run_agents_as_tools(self, workflow: Workflow, input_data: Any) -> Any:
        """Run orchestrator-workers pattern"""
        orchestrator = self.agents.get(workflow.agents[0])
        
        # Orchestrator decides which worker agents to call
        plan = await orchestrator.plan(input_data)
        
        # Execute worker agents based on plan
        results = {}
        for step in plan["steps"]:
            agent = self.agents.get(step["agent_id"])
            worker_input = self._map_inputs({"original_input": input_data, "results": results}, step.get("input_mapping"))
            result = await agent.execute(worker_input)
            results[step["name"]] = result
        
        # Orchestrator produces final response
        return await orchestrator.synthesize(results)
    
    def _map_inputs(self, context: Dict, mapping: Optional[Dict[str, str]]) -> Any:
        """Map inputs from context based on mapping config"""
        if not mapping:
            return context.get("last_output") or context.get("input")
        
        return {k: context.get(v) for k, v in mapping.items()}
    
    def _combine_results(self, results: List[Any]) -> Any:
        """Combine parallel results - can be overridden"""
        return {"results": results, "count": len(results)}
    
    def _aggregate_votes(self, results: List[Any]) -> Any:
        """Aggregate K-voting results - majority wins"""
        # Simple implementation: return most common result
        from collections import Counter
        return Counter(str(r) for r in results).most_common(1)[0][0]


---

## 8. Workflow Types

Since agents can be defined with decorators OR YAML that compiles to decorators, workflow types determine how agents combine:

| workflow | Description |
|----------|-------------|
| `single` (default) | Single agent - just this agent |
| `chain` | Sequential: agent1 → agent2 → agent3 |
| `parallel` | Fan-out: [agent1, agent2, agent3] run simultaneously |
| `agents_as_tools` | Orchestrator calls others as tools |

### Workflow Details

**single** (default):
```yaml
# Single agent - no workflow specified
helper:
  instruction: "You are helpful."
  model: sonnet
```

**chain** - Sequential execution:
```yaml
# Runs order_agent first, then ship_agent
order_and_ship:
  workflow: chain
  agents: [order_agent, ship_agent]
  # Output of order_agent becomes input to ship_agent
```

**parallel** - Fan-out execution:
```yaml
# All agents run simultaneously, results combined
research_team:
  workflow: parallel
  agents: [web_searcher, data_fetcher, analyzer]
  # Each agent gets the same input, results aggregated
```

**agents_as_tools** - Orchestrator pattern:
```yaml
# Coordinator can call other agents as tools
coordinator:
  workflow: agents_as_tools
  agents: [order_agent, ship_agent, inventory_agent]
  # Coordinator decides which agents to call and when
```

```yaml
agents:
  # Single agent (default)
  helper:
    instruction: "You are helpful."
    
  # Chain workflow - sequential
  researcher:
    workflow: chain
    agents: [searcher, analyzer, writer]
    
  # Parallel workflow - fan-out/in
  scout:
    workflow: parallel
    agents: [searcher1, searcher2, searcher3]
    
  # Orchestrator-workers
  pmo:
    workflow: agents_as_tools
    agents: [ny_manager, london_manager]
```

### Skills (FastAgent Built-in)

pyclopse uses FastAgent's built-in skills system (`fast_agent.skills`). The custom skills system has been removed.

**Key Points:**
- Skills are defined in FastAgent and loaded automatically
- Use FastAgent's skill registry to manage skills
- Skills provide additional capabilities to agents (e.g., web search, file operations)
- pyclopse no longer has its own skills module - use FastAgent skills

```python
# Example: Using FastAgent skills
from fast_agent import FastAgent
from fast_agent.skills import skill

# Define a skill
@skill(name="web_search")
def web_search(query: str):
    """Search the web for information"""
    # Implementation
    pass

# Agent automatically has access to the skill
fast = FastAgent("my_agent")

@fast.agent(
    name="researcher",
    instruction="Research topics using available skills",
    skills=["web_search"],  # Attach skills to agent
)
async def main():
    async with fast.run() as agent:
        await agent.interactive()
```

For YAML-defined agents, skills are specified via the `skills` field:

```yaml
agents:
  researcher:
    instruction: "Research the given topic"
    skills:
      - web_search
      - fetch
      - time
```

### 8.1 MCP Tools as Agent Tools

FastAgent provides native MCP support. Agents can use MCP tools from the registry:

FastAgent provides native MCP support. Agents can use MCP tools from the registry:

```python
# pyclopse/workflows/mcp_integration.py
from typing import Dict, Any, List

class MCPToolRegistry:
    """Registry of available MCP tools for agents"""
    
    def __init__(self):
        self.tools: Dict[str, Any] = {}
    
    def register(self, name: str, tool: Any):
        """Register an MCP tool"""
        self.tools[name] = tool
    
    def get_tools_for_agent(self, agent_id: str) -> List[str]:
        """Get list of tools available to an agent"""
        # Load from agent config
        agent_config = self._load_agent_config(agent_id)
        return agent_config.get("tools", [])
    
    async def execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """Execute an MCP tool"""
        tool = self.tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")
        return await tool.execute(args)
```

### 8.4 Built-in Workflow Patterns

```python
# pyclopse/workflows/patterns.py
from .runner import WorkflowRunner, Workflow, WorkflowType
from typing import Any, Dict

class WorkflowPatterns:
    """Pre-built workflow patterns"""
    
    @staticmethod
    def research_write() -> Workflow:
        """Research → Write chain"""
        return Workflow(
            name="research_write",
            type=WorkflowType.CHAIN,
            agents=["research_agent", "writer_agent"],
            steps=[
                WorkflowStep(agent_id="research_agent", output_key="research"),
                WorkflowStep(
                    agent_id="writer_agent",
                    input_mapping={"context": "research"}
                )
            ]
        )
    
    @staticmethod
    def parallel_scrape(urls: List[str]) -> Workflow:
        """Scrape multiple URLs in parallel"""
        return Workflow(
            name="parallel_scrape",
            type=WorkflowType.PARALLEL,
            agents=["scraper_agent"] * len(urls),
            steps=[
                WorkflowStep(
                    agent_id="scraper_agent",
                    input_mapping={"url": f"url_{i}"}
                )
                for i in range(len(urls))
            ]
        )
    
    @staticmethod
    def code_review_k_way() -> Workflow:
        """K-way code review for error reduction"""
        return Workflow(
            name="code_review",
            type=WorkflowType.MAKER,
            agents=["reviewer_agent"],
            k_votes=3
        )


### 8.1 Plugin Loader

```python
# pyclopse/plugins/loader.py
import importlib.util
from pathlib import Path
from typing import Dict, Any

class PluginLoader:
    def __init__(self, plugin_dirs: List[Path]):
        self.plugin_dirs = plugin_dirs
    
    def discover_plugins(self) -> Dict[str, Path]:
        """Find all plugins in plugin directories"""
        plugins = {}
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                continue
            for entry in plugin_dir.iterdir():
                if entry.is_dir() and (entry / "plugin.py").exists():
                    plugins[entry.name] = entry
        return plugins
    
    def load_plugin(self, name: str, path: Path, config: Dict[str, Any]):
        """Load a plugin module"""
        spec = importlib.util.spec_from_file_location(
            f"pyclopse.plugins.{name}",
            path / "plugin.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

### 8.2 Route Dispatch System

Plugins don't run their own servers—they register routes with the main FastAPI app. This is cleaner than OpenClaw's approach where each plugin could run as a separate process.

```python
# Plugin just registers routes, doesn't run its own server
class TelegramPlugin(Plugin):
    def register_routes(self, app: FastAPI) -> None:
        app.add_api_route("/telegram/webhook", self.handle_webhook)
        app.add_api_route("/telegram/commands", self.handle_commands)
    
    async def handle_webhook(self, request: Request):
        # Handle incoming Telegram messages
        pass
    
    async def handle_commands(self, request: Request):
        # Handle Telegram commands
        pass

class DiscordPlugin(Plugin):
    def register_routes(self, app: FastAPI) -> None:
        app.add_api_route("/discord/webhook", self.handle_webhook)
        app.add_api_route("/discord/interactions", self.handle_interactions)
```

The main gateway dispatches requests to the appropriate plugin:

```python
# Main router - plugins register routes at startup
app = FastAPI()

# Plugins register their routes during initialization
telegram_plugin = TelegramPlugin(config)
telegram_plugin.register_routes(app)

discord_plugin = DiscordPlugin(config)
discord_plugin.register_routes(app)

# Resulting routes:
# POST /telegram/webhook
# POST /telegram/commands
# POST /discord/webhook
# POST /discord/interactions
```

### 8.3 Multi-Language Plugin Support

**The Problem:** OpenClaw plugins are TypeScript-only since they're part of the same Node.js process.

**pyclopse's Solution:** Plugins are NOT limited to Python—any language can be a plugin via HTTP/RPC.

#### Architecture

- pyclopse runs on ONE port (e.g., 18789)
- Plugins register ROUTES at startup (not their own servers!)
- Main router dispatches: `/telegram/*` → Telegram plugin, `/discord/*` → Discord plugin

#### Plugin Interface (any language)

Plugins implement a simple HTTP interface:

```
POST /webhook - receive messages from channel
POST /send   - send messages to channel
GET  /health - liveness check
```

Example external plugin in any language:

```python
# Example: Go plugin (external_service.go)
package main

import (
    "github.com/gin-gonic/gin"
)

func main() {
    r := gin.Default()
    
    r.POST("/webhook", func(c *gin.Context) {
        // Receive messages from channel
        c.JSON(200, gin.H{"status": "received"})
    })
    
    r.POST("/send", func(c *gin.Context) {
        // Send messages to channel
        c.JSON(200, gin.H{"status": "sent"})
    })
    
    r.GET("/health", func(c *gin.Context) {
        c.JSON(200, gin.H{"status": "healthy"})
    })
    
    r.Run(":8080")
}
```

```rust
// Example: Rust plugin (external_service.rs)
use actix_web::{web, App, HttpServer, Responder};

async fn webhook(req: web::HttpRequest) -> impl Responder {
    "received"
}

async fn send(req: web::HttpRequest) -> impl Responder {
    "sent"
}

async fn health() -> impl Responder {
    "healthy"
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    HttpServer::new(|| {
        App::new()
            .route("/webhook", web::post().to(webhook))
            .route("/send", web::post().to(send))
            .route("/health", web::get().to(health))
    })
    .bind("127.0.0.1:8081")?
    .run()
    .await
}
```

#### Benefits

- **Any language can be a plugin**: Python, Go, Rust, Node.js, etc.
- **One port to manage**: pyclopse runs on a single port
- **Easy to proxy**: Behind nginx, Caddy, or any reverse proxy
- **Plugins distributed as binaries**: No language runtime required for plugin consumers

#### Plugin Registration

External plugins register with the gateway:

```python
# pyclopse/plugins/registry.py
from typing import Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class ExternalPlugin:
    name: str
    base_url: str  # e.g., "http://localhost:8081"
    health_check_interval: int = 30
    
class PluginRegistry:
    def __init__(self):
        self.plugins: Dict[str, ExternalPlugin] = {}
    
    def register(self, plugin: ExternalPlugin):
        """Register an external plugin"""
        self.plugins[plugin.name] = plugin
    
    def get_plugin(self, name: str) -> Optional[ExternalPlugin]:
        return self.plugins.get(name)
    
    async def check_health(self, name: str) -> bool:
        """Check if plugin is healthy"""
        plugin = self.get_plugin(name)
        if not plugin:
            return False
        # GET /health on the plugin's base_url
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{plugin.base_url}/health")
                return resp.status_code == 200
            except:
                return False
```

### 8.2 Hook System

```python
# pyclopse/plugins/hooks.py
from typing import Callable, Dict, List, Any
from enum import Enum

class HookPhase(str, Enum):
    BEFORE_AGENT_START = "before_agent_start"
    AFTER_AGENT_RESPONSE = "after_agent_response"
    BEFORE_TOOL_EXEC = "before_tool_exec"
    AFTER_TOOL_EXEC = "after_tool_exec"
    ON_MESSAGE = "on_message"

HookHandler = Callable[..., Any]

class HookRegistry:
    def __init__(self):
        self.hooks: Dict[HookPhase, List[HookHandler]] = {
            phase: [] for phase in HookPhase
        }
    
    def register(self, phase: HookPhase, handler: HookHandler):
        self.hooks[phase].append(handler)
    
    async def run(self, phase: HookPhase, context: Dict[str, Any]):
        for handler in self.hooks[phase]:
            await handler(context)
```

---

## 9. Gateway Concepts

These are gateway-level features that pyclopse must handle, which are outside the scope of FastAgent's agent/workflow engine:

### 9.1 Compaction

**What**: Compress session context to stay within token limits.

**When**: Before hitting token limits during a conversation.

**How**: Summarize old messages while keeping recent context intact. This is critical for long-running sessions where the full message history would exceed the model's context window.

```python
# pyclopse/core/compaction.py
class ContextCompactor:
    def __init__(self, max_tokens: int = 100000):
        self.max_tokens = max_tokens
    
    async def compact(self, messages: list) -> list:
        """Compress messages to fit within token budget"""
        # Keep recent messages intact
        # Summarize older messages into a summary message
        # Return compacted message list
        pass
    
    async def summarize_messages(self, messages: list) -> str:
        """Use the model to summarize old message history"""
        pass
```

### 9.2 Session Persistence

**What**: Save and restore session state for crash recovery and restarts.

**When**: On gateway restart, before potential crash, and periodically.

**How**: Serialize messages, agent state, and metadata to disk (SQLite or JSON).

```python
# pyclopse/core/session.py
class SessionPersistence:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
    
    async def save_session(self, session: Session):
        """Serialize session to disk"""
        pass
    
    async def load_session(self, session_id: str) -> Session:
        """Restore session from disk"""
        pass
    
    async def list_sessions(self) -> list:
        """List all persisted sessions"""
        pass
```

### 9.3 Context Windows

**What**: Manage token limits per session, tracking message sizes and truncating as needed.

**When**: Always - ongoing management throughout the session.

**How**: Track message token counts, monitor context usage, and proactively truncate or compact before hitting limits.

```python
# pyclopse/core/context.py
class ContextManager:
    def __init__(self, session: Session, max_tokens: int):
        self.session = session
        self.max_tokens = max_tokens
    
    def count_tokens(self, messages: list) -> int:
        """Count tokens in message list"""
        pass
    
    def needs_truncation(self) -> bool:
        """Check if context needs truncation"""
        pass
    
    def truncate(self, messages: list) -> list:
        """Truncate oldest messages to fit budget"""
        pass
```

### 9.4 Pulse System

**What**: Periodic agent polling for background tasks (heartbeats).

**When**: Configured intervals (e.g., every 30 minutes).

**How**: Asyncio tasks per agent that run at configured intervals, checking for updates or performing background work.

> **Status**: Already implemented in `pyclopse/pulse/` module.

```python
# pyclopse/pulse/runner.py
class PulseRunner:
    def __init__(self, agent: Agent, config: PulseConfig):
        self.agent = agent
        self.config = config
    
    async def start(self):
        """Start periodic pulse tasks"""
        pass
    
    async def pulse(self):
        """Execute pulse task"""
        pass
```

### 9.5 Jobs System

**What**: Scheduled tasks (cron-like functionality).

**When**: Based on cron schedules configured per job.

**How**: Job queue with persistence (SQLite), scheduler runs jobs at configured times.

> **Status**: Already implemented in `pyclopse/jobs/` module.

```python
# pyclopse/jobs/scheduler.py
class JobScheduler:
    def __init__(self, job_store: JobStore):
        self.job_store = job_store
    
    async def schedule(self, job: Job):
        """Add job to schedule"""
        pass
    
    async def run_pending(self):
        """Execute pending jobs"""
        pass
```

### 9.6 Channel Management

**What**: Integration with messaging platforms (Telegram, Discord, Slack, WhatsApp, etc.).

**When**: Always running - gateway must handle incoming messages from all channels.

**How**: Channel adapters register with the gateway, receive webhooks, and route messages to agents.

> **Implemented**: See Section 5. Channel Adapters.

### 9.7 Security Layer

**What**: Exec approvals, audit logging, and sandboxing for dangerous operations.

**When**: Before any dangerous operation (exec, file writes, network calls).

**How**: Allowlist/denylist policies, approval UI for pending requests, comprehensive audit logging.

> **Implemented**: See Section 4. Security Model.

### 9.8 Memory (ClawVault)

**What**: Long-term memory integration for persistent context.

**When**: On session end (store context), on session start (restore context), on query (search memory).

**How**: CLI wrapper for clawvault npm package (subprocess calls).

> **Implemented**: See Section 6. Memory Integration.

---

## 10. Mobile API Design

### 9.1 FastAPI Application

```python
# pyclopse/api/app.py
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional
import jwt

app = FastAPI(title="pyclopse API", version="0.1.0")
security = HTTPBearer()

# Request/Response Models
class SendMessageRequest(BaseModel):
    channel: str
    target: str
    content: str

class MessageResponse(BaseModel):
    id: str
    channel: str
    sender: str
    content: str
    timestamp: str

class AgentConfig(BaseModel):
    model: Optional[str] = None
    temperature: Optional[float] = None
    maxTokens: Optional[int] = None
    systemPrompt: Optional[str] = None

# Auth
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, "secret", algorithms=["HS256"])
        return payload
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Routes
@app.post("/api/v1/sessions")
async def create_session(user=Depends(get_current_user)):
    pass

@app.get("/api/v1/sessions/{session_id}")
async def get_session(session_id: str, user=Depends(get_current_user)):
    pass

@app.post("/api/v1/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    request: SendMessageRequest,
    user=Depends(get_current_user)
):
    pass

@app.get("/api/v1/agents")
async def list_agents(user=Depends(get_current_user)):
    pass

@app.patch("/api/v1/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    config: AgentConfig,
    user=Depends(get_current_user)
):
    pass

@app.get("/api/v1/channels")
async def list_channels(user=Depends(get_current_user)):
    pass

@app.get("/api/v1/memory/search")
async def search_memory(
    query: str,
    limit: int = 10,
    user=Depends(get_current_user)
):
    pass
```

---

## 10. Implementation Roadmap

### Phase 1: Foundation (Weeks 1-2)

- [ ] Set up project structure with uv
- [ ] Implement YAML config loading with Pydantic
- [ ] Create basic logging system
- [ ] Build security module (approvals, audit)
- [ ] Create channel adapter base class

### Phase 2: Core Systems (Weeks 3-4)

- [ ] Implement message routing
- [ ] Build session management
- [ ] Create agent class with basic tool execution
- [ ] Implement memory client (ClawVault)
- [ ] Build provider abstraction layer

### Phase 3: Channels (Weeks 5-6)

- [ ] Implement Telegram adapter
- [ ] Implement Discord adapter
- [ ] Implement Slack adapter
- [ ] Implement WhatsApp adapter
- [ ] Add remaining channel adapters

### Phase 4: Jobs & Pulse (Weeks 7-8)

- [ ] Build job scheduler system
- [ ] Implement job persistence
- [ ] Create pulse runner
- [ ] Add pulse triggers
- [ ] Build active hours support

### Phase 5: Plugins & Hooks (Weeks 9-10)

- [ ] Implement plugin loader
- [ ] Build hook registry
- [ ] Create plugin HTTP endpoints
- [ ] Add built-in hooks (gmail-watcher, etc.)

### Phase 6: TUI & API (Weeks 11-12)

- [ ] Build TUI with Textual
- [ ] Implement chat view
- [ ] Create REST API with FastAPI
- [ ] Add authentication
- [ ] Mobile app API endpoints

### Phase 7: Testing & Polish (Weeks 13-14)

- [ ] Write unit tests
- [ ] Integration tests
- [ ] Security audit implementation
- [ ] Documentation
- [ ] Performance optimization

---

## 11. Migration from OpenClaw

### 11.1 Config Conversion

Provide a tool to convert OpenClaw JSON config to pyclopse YAML:

```python
# scripts/convert_config.py
import json
import yaml
from pathlib import Path

def convert_config(input_file: Path, output_file: Path):
    with open(input_file) as f:
        config = json.load(f)
    
    # Convert to pyclopse format
    pyclopse_config = {
        "version": "1.0",
        "gateway": {...},
        "security": {...},
        # ... map fields
    }
    
    with open(output_file, "w") as f:
        yaml.dump(pyclopse_config, f, default_flow_style=False)
```

### 11.2 Data Migration

- Sessions: Export to JSON, import to new format
- Memory: Continue using ClawVault (no migration needed)
- Plugins: May need updates for Python compatibility

---

## 12. Key Differences from OpenClaw

| Aspect | OpenClaw | pyclopse |
|--------|----------|--------|
| Language | TypeScript | Python |
| Package Manager | npm | uv |
| Config Format | JSON | YAML |
| Config Validation | Zod | Pydantic |
| TUI | blessed + unknown | Textual |
| API Server | custom | FastAPI |
| Memory | ClawVault (CLI) | ClawVault (CLI) |
| Sandboxing | Docker | Docker |

---

## 13. Dependencies

### Core Dependencies

- **pydantic**: Config validation
- **pyyaml**: YAML parsing
- **fastapi**: REST API
- **uvicorn**: ASGI server
- **httpx**: HTTP client
- **textual**: TUI framework

### Security Dependencies

- **cryptography**: Encryption
- **jwt**: Token handling
- **passlib**: Password hashing

### Database

- **aiosqlite**: SQLite async support (for job/session persistence)

---

## 14. Testing Strategy

```python
# tests/test_approvals.py
import pytest
from pyclopse.security.approvals import ExecApprovalSystem, ApprovalRequest

@pytest.fixture
def approval_system():
    config = type("Config", (), {
        "mode": "allowlist",
        "safe_bins": ["/bin/ls", "/bin/cat"],
        "always_approve": ["git status"]
    })()
    return ExecApprovalSystem(config)

@pytest.mark.asyncio
async def test_always_approve(approval_system):
    request = ApprovalRequest(
        command="git status",
        args=[],
        cwd="/home",
        agent_id="test",
        session_id="test"
    )
    assert await approval_system.should_approve(request) is True

@pytest.mark.asyncio
async def test_safe_bin(approval_system):
    request = ApprovalRequest(
        command="/bin/ls",
        args=["-la"],
        cwd="/home",
        agent_id="test",
        session_id="test"
    )
    assert await approval_system.should_approve(request) is True
```

---

## 15. Future Considerations

- **Multi-agent support**: Expand beyond single-agent architecture
- **Distributed processing**: Support multiple gateway instances
- **Plugin marketplace**: Publish and install plugins
- **Web UI**: Add web-based management interface
- **Voice support**: Integrate speech-to-text and text-to-speech
