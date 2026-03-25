# TUI Dashboard

**Files:** `pyclawops/tui/dashboard.py`, `pyclawops/tui/screens.py`,
`pyclawops/tui/widgets.py`, `pyclawops/tui/components/`
**Library:** Textual

Run with: `pyclawops run` (default). Use `pyclawops run --headless` to skip.

---

## Layout

```
┌─────────────────────────────────────────┐
│  Header: pyclawops vX.Y.Z  │  status bar   │
├─────────────────────────────────────────┤
│  Tab strip: 0-9, t                      │
├─────────────────────────────────────────┤
│                                         │
│  Detail pane (changes per tab)          │
│                                         │
├─────────────────────────────────────────┤
│  Log pane (live gateway log stream)     │
└─────────────────────────────────────────┘
```

---

## Tabs

| Key | Tab | Content |
|-----|-----|---------|
| `0` | Agents | Agent list with status |
| `1` | Sessions | Active sessions DataTable |
| `2` | History | Message history for selected session |
| `3` | Jobs | Jobs with status and next-run |
| `4` | Sys-Prompt | System prompt for selected agent |
| `5` | Config | Current config (redacted) |
| `6` | Files | File browser for `~/.pyclawops/` |
| `7` | Skills | Discovered skills |
| `8` | Run-Hist | Job run history |
| `9` | Agent-Log | Per-agent log viewer |
| `t` | Traces | OpenTelemetry span viewer |

---

## Key Bindings

| Key | Action |
|-----|--------|
| `0`–`9`, `t` | Switch tabs |
| `r` | Refresh tab |
| `v` | View detail for selection |
| `e` | Edit selection |
| `[` / `]` | Resize panes |
| `Ctrl+S` | Save |
| `F5` | Force full refresh |
| `q` / `Ctrl+C` | Quit |

---

## Gateway Relationship

The TUI shares the same gateway instance — `run_dashboard(gateway)` drives the
Textual app in the same process. The TUI reads `gateway._usage`,
`gateway._agent_manager`, `gateway._session_manager` directly. Status bar
updates via `_update_status_bar()` on each tick.

## Log Drain

`_QueueLogHandler` puts log records into an `asyncio.Queue`. The log pane
drains this queue and renders live records without blocking the event loop.
