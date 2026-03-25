# Channel Adapters

**Files:** `pyclawops/channels/`, `pyclawops/channels/plugin.py`,
`pyclawops/channels/loader.py`

Channel adapters are the boundary between external messaging platforms and the
Gateway. Each adapter receives messages from a platform and calls
`gateway.dispatch()` to route them inward; replies flow back via the adapter.

---

## ChannelPlugin ABC (`pyclawops/channels/plugin.py`)

All channel adapters implement `ChannelPlugin`:

```python
class ChannelPlugin(ABC):
    async def start(self, gateway_handle: GatewayHandle) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, channel: str, user_id: str, text: str, **kwargs) -> None: ...
```

`GatewayHandle` is the narrow interface plugins use to talk to the Gateway:

```python
class GatewayHandle:
    async def dispatch(
        self, channel: str, user_id: str, user_name: str, text: str,
        message_id: str | None = None,
    ) -> str | None: ...
```

---

## Built-in Adapters

| Module | Channel | Key features |
|--------|---------|--------------|
| `telegram.py` | Telegram | Multi-bot, polling, message splitting, typing indicator, topics |
| `slack.py` | Slack | Events API, threading, pulse heartbeat |
| `discord.py` | Discord | Bot API |
| `googlechat.py` | Google Chat | Webhook |
| `imessage.py` | iMessage | macOS AppleScript bridge |
| `line.py` | LINE | Messaging API |
| `signal.py` | Signal | signal-cli |
| `whatsapp.py` | WhatsApp | Cloud API |

---

## Telegram (`pyclawops/channels/telegram.py`)

### Multi-bot

`gateway._tg_bots: dict[str, Bot]` — one `python-telegram-bot` Bot per token.
Each bot runs in its own polling `asyncio.Task`.

### Message Splitting

Telegram's 4096-character limit requires splitting long responses. The splitter
tries boundaries in order: paragraph break (`\n\n`), single newline, then hard
character boundary. HTML tags are never split mid-tag.

### Thinking Tag Display

When `show_thinking=True`, the agent's reasoning content is formatted as a
Telegram `<blockquote expandable>` HTML spoiler — collapsed by default,
expandable on tap. Reasoning + response are combined into a single edit to the
message placeholder.

### Typing Indicator

Sent immediately on message receipt, then refreshed every 4 seconds in a
background loop (Telegram's typing status expires after 5 seconds). The loop
is cancelled when the response is ready.

### Topic Support

Telegram forum groups (supergroups with topics enabled) use `message_thread_id`
to route messages to specific topics. Configured via `topics:` in agent config.

---

## Slack (`pyclawops/channels/slack.py`)

### Threading

When `threading: true` in `SlackConfig`, each Slack thread becomes its own
pyclawops session. Session key: `thread_ts or ts` (the thread root timestamp).
Replies include `thread_ts` so they stay in the thread.

### Pulse

Optional heartbeat to `SlackConfig.pulse_channel`. Sends a brief status
message on a configurable interval — useful for monitoring that the gateway
is alive.

---

## Plugin Discovery

Third-party channel plugins are discovered via:

1. **Entry points** — `pyclawops.channels` entry point group in `pyproject.toml`
2. **Explicit config** — `plugins.channels` list in `pyclawops.yaml`

Plugins receive a `GatewayHandle` at `start()` and use it to dispatch inbound
messages. The Gateway calls `send()` to deliver outbound replies.

---

## Per-Channel Security

Each channel can override the global security config:

```yaml
channels:
  telegram:
    allowedUsers: [123456789]   # only these Telegram user IDs
    deniedUsers: []
  slack:
    allowedUsers: ["U123456"]   # Slack user IDs are strings
    deniedUsers: []
```

The Gateway checks these before dispatching. Denied messages are silently
dropped. `SecurityConfig.denied_users` is `List[int]` (Telegram); Slack uses
`List[str]`.
