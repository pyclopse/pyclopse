# Security

**Files:** `pyclaw/security/approvals.py`, `pyclaw/security/sandbox.py`,
`pyclaw/security/audit.py`

---

## Exec Approvals (`pyclaw/security/approvals.py`)

Controls whether a `bash` tool call is allowed. Evaluated before every shell
execution.

### Modes

| Mode | Behaviour |
|------|-----------|
| `allowlist` | Only commands matching `safe_bins` are allowed |
| `denylist` | Commands matching `deny_list` are blocked; all others allowed |
| `all` | All commands permitted |
| `none` | All commands denied |

### always_approve

Patterns that bypass the mode check entirely:
```yaml
security:
  execApprovals:
    mode: allowlist
    safeBins: [ls, cat, python3, uv]
    alwaysApprove: ["uv run *.py"]
```

Patterns can be literal binary names or regex strings matched against the full
command.

---

## Sandbox (`pyclaw/security/sandbox.py`)

When `security.sandbox.enabled: true`, shell commands run inside Docker:

- Network: `none`
- Memory: configurable (default 256MB)
- CPU: fractional quota (default 0.5)
- PIDs: max 50
- Filesystem: read-only rootfs + tmpfs at `/tmp`

```yaml
security:
  sandbox:
    enabled: true
    image: python:3.12-slim
    memoryMb: 256
```

`create_sandbox(config)` factory returns `DockerSandbox` or `NoOpSandbox`.

---

## Audit Logger (`pyclaw/security/audit.py`)

Appends JSON-lines records to `~/.pyclaw/logs/audit.log`.

Records: inbound messages, tool executions, outbound replies, session events.

`audit_log_tail(n)` and `audit_log_search(query)` MCP tools expose the log
to agents for self-monitoring.

```yaml
security:
  audit:
    enabled: true
    retentionDays: 30
```

---

## Per-Channel Security

```yaml
channels:
  telegram:
    allowedUsers: [123456789]   # int — Telegram user IDs
  slack:
    allowedUsers: ["U123ABC"]   # str — Slack user IDs
```

`SecurityConfig.denied_users` is `List[int]` (Telegram).
`SlackConfig.allowed_users` is `List[str]` (Slack). This difference reflects
each platform's native ID format.
