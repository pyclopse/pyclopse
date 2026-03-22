"""Gateway Dashboard TUI for pyclaw.

Three-pane layout:
  ┌────────────────────────────────────────────────────────┐
  │  Header (title + clock)                                │
  │  [Agent1] [Agent2] ...  ← agent tab strip             │
  │  [Sessions][History][Jobs][SysPrompt][Config]...       │
  │  Status: uptime | messages | active jobs               │
  ├────────────────────────────────────────────────────────┤
  │  Detail pane  (resizable)                              │
  ├────────────────────────────────────────────────────────┤
  │  Log pane — live gateway logs  (resizable)             │
  └────────────────────────────────────────────────────────┘

Key bindings:
  0  Agent Card  1  Sessions   2  History   3  Jobs   4  Sys Prompt
  5  Config      6  Files      7  Skills    8  Run Hist   9  Agent Log
  t  OTel Traces
  h  Load history for selected session
  r  Run selected job now
  v  View run history for selected job
  e  Edit selected file (Files view only)
  Ctrl+S  Save file  |  Escape  Cancel edit
  [  Shrink log pane    ]  Grow log pane
  F5 Refresh current view   q  Quit
"""
from __future__ import annotations

import json
import logging
import queue
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    Tab,
    Tabs,
    TextArea,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────── Log Handler ─────────────────────────────────

_LOG_QUEUE: "queue.SimpleQueue[str]" = queue.SimpleQueue()

# Dirs / extensions excluded from the file browser
_BROWSER_EXCLUDE_DIRS = frozenset({
    ".venv", "__pycache__", ".git", "node_modules",
    "sessions", "runs", "logs", ".fast-agent",
})
_BROWSER_EXCLUDE_EXTS = frozenset({".pyc", ".pyo", ".bak"})


class _QueueLogHandler(logging.Handler):
    """Puts formatted log records onto a SimpleQueue for the TUI to drain."""

    def __init__(self, q: "queue.SimpleQueue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


# ─────────────────────────────── Helpers ─────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    else:
        return f"{n / 1024 / 1024:.1f}M"


# ─────────────────────────────── View: Sessions ──────────────────────────────


class SessionsView(Vertical):
    """DataTable listing all sessions for the active agent."""

    DEFAULT_CSS = """
    SessionsView {
        height: 1fr;
        overflow: hidden hidden;
    }
    SessionsView #sessions-hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
        background: $panel-darken-1;
    }
    SessionsView #sessions-table {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="sessions-hint")
        yield DataTable(id="sessions-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#sessions-table", DataTable)
        t.add_columns("Session ID", "Channel", "User", "Msgs", "Tokens", "Updated", "Active")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        sessions: List[Any] = []
        context_window: Optional[int] = None
        try:
            sm = getattr(self.gateway, "_session_manager", None)
            if sm and self._agent_id:
                sessions = sm.list_sessions_sync(agent_id=self._agent_id)
            am = getattr(self.gateway, "_agent_manager", None)
            if am:
                agent = am.agents.get(self._agent_id)
                if agent:
                    context_window = getattr(agent.config, "context_window", None)
        except Exception as e:
            logger.debug(f"SessionsView load error: {e}")
        self.app.call_from_thread(self._populate, sessions, context_window)

    def _populate(self, sessions: List[Any], context_window: Optional[int]) -> None:
        t = self.query_one("#sessions-table", DataTable)
        hint = self.query_one("#sessions-hint", Static)
        t.clear()
        for s in sessions:
            updated = s.updated_at.strftime("%m-%d %H:%M") if s.updated_at else ""
            active = "yes" if getattr(s, "is_active", False) else "no"
            ctx = getattr(s, "context", {}) or {}
            ctx_tokens = ctx.get("_ctx_tokens", 0) or 0
            if context_window and context_window > 0 and ctx_tokens:
                pct = ctx_tokens / context_window * 100
                tok_str = f"{ctx_tokens:,}/{context_window:,} ({pct:.0f}%)"
            elif ctx_tokens:
                tok_str = f"{ctx_tokens:,}"
            else:
                tok_str = "—"
            t.add_row(
                s.id,
                s.channel or "",
                str(s.user_id or ""),
                str(s.message_count),
                tok_str,
                updated,
                active,
                key=s.id,
            )
        count = len(sessions)
        hint.update(
            f"{count} session{'s' if count != 1 else ''} — agent: {self._agent_id}"
            "  \\[h = load history for selected]"
        )

    def get_selected_session_id(self) -> Optional[str]:
        t = self.query_one("#sessions-table", DataTable)
        if t.cursor_row is None:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None


# ─────────────────────────────── View: History ───────────────────────────────


class HistoryView(Vertical):
    """Displays raw FastAgent conversation history JSON for a selected session."""

    DEFAULT_CSS = """
    HistoryView {
        height: 1fr;
        overflow: hidden hidden;
    }
    HistoryView #history-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    HistoryView #history-log {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""
        self._session_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static(
            "Select a session in Sessions view then press h to load history.",
            id="history-bar",
        )
        yield RichLog(
            id="history-log",
            auto_scroll=False,
            markup=False,
            highlight=True,
        )

    def load_session(self, agent_id: str, session_id: str) -> None:
        self._agent_id = agent_id
        self._session_id = session_id
        bar = self.query_one("#history-bar", Static)
        bar.update(f"Loading history for {session_id} …")
        self._do_load()

    @work(thread=True)
    def _do_load(self) -> None:
        content = ""
        error = ""
        try:
            sm = getattr(self.gateway, "_session_manager", None)
            if sm:
                sessions = sm.list_sessions_sync(agent_id=self._agent_id)
                session = next(
                    (s for s in sessions if s.id == self._session_id), None
                )
                if session and session.history_path and session.history_path.exists():
                    raw = session.history_path.read_text()
                    data = json.loads(raw)
                    content = json.dumps(data, indent=2)
                else:
                    error = f"No history.json found for session: {self._session_id}"
        except Exception as e:
            error = f"Error: {e}\n{traceback.format_exc()}"
        self.app.call_from_thread(self._display, content, error)

    def _display(self, content: str, error: str) -> None:
        log = self.query_one("#history-log", RichLog)
        bar = self.query_one("#history-bar", Static)
        log.clear()
        if error:
            bar.update(f"Error — {error[:120]}")
            log.write(error)
        else:
            bar.update(
                f"Session: {self._session_id}  "
                f"({len(content):,} chars)  \\[scroll with arrow keys / page up/down]"
            )
            log.write(content)


# ─────────────────────────────── View: Jobs ──────────────────────────────────


class JobsView(Vertical):
    """DataTable of scheduled jobs for the active agent with Run Now support."""

    DEFAULT_CSS = """
    JobsView {
        height: 1fr;
        overflow: hidden hidden;
    }
    JobsView #jobs-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    JobsView #jobs-table {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="jobs-bar")
        yield DataTable(id="jobs-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#jobs-table", DataTable)
        t.add_columns("Name", "Schedule", "Enabled", "Status", "Next Run", "Last Run", "Runs")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load()

    def _load(self) -> None:
        t = self.query_one("#jobs-table", DataTable)
        bar = self.query_one("#jobs-bar", Static)
        t.clear()

        js = getattr(self.gateway, "_job_scheduler", None)
        if not js:
            bar.update("Job scheduler not available")
            return

        job_agent_map: dict = getattr(js, "_job_agents", {})
        agent_jobs = [
            job
            for job_id, job in js.jobs.items()
            if job_agent_map.get(job_id) == self._agent_id
        ]

        for job in agent_jobs:
            sched = getattr(job, "schedule", None)
            if sched is not None:
                kind = getattr(sched, "kind", "")
                if kind == "cron":
                    schedule_str = f"cron: {getattr(sched, 'expr', '')}"
                elif kind == "interval":
                    secs = getattr(sched, "seconds", 0)
                    schedule_str = f"every {secs}s"
                else:
                    schedule_str = str(sched)
            else:
                schedule_str = "—"

            enabled = "yes" if getattr(job, "enabled", True) else "no"
            running = getattr(js, "_running_jobs", set())
            status = "running" if job.id in running else "idle"

            next_run = ""
            if getattr(job, "next_run", None):
                next_run = job.next_run.strftime("%m-%d %H:%M")

            last_run = ""
            if getattr(job, "last_run", None):
                last_run = job.last_run.strftime("%m-%d %H:%M")

            run_count = getattr(job, "run_count", 0)

            t.add_row(
                job.name or job.id,
                schedule_str,
                enabled,
                status,
                next_run,
                last_run,
                str(run_count),
                key=job.id,
            )

        count = len(agent_jobs)
        bar.update(
            f"{count} job{'s' if count != 1 else ''} — agent: {self._agent_id}"
            "  \\[r = run now  v = view run history]"
        )

    def get_selected_job_id(self) -> Optional[str]:
        t = self.query_one("#jobs-table", DataTable)
        if t.cursor_row is None:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None


# ─────────────────────────────── View: System Prompt ─────────────────────────


class SystemPromptView(ScrollableContainer):
    """Shows the reconstructed system prompt for the active agent."""

    DEFAULT_CSS = """
    SystemPromptView {
        height: 1fr;
        overflow: hidden hidden;
    }
    SystemPromptView #sysprompt-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    SystemPromptView #sysprompt-text {
        padding: 0 2;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="sysprompt-bar")
        yield Static("", id="sysprompt-text")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        bar = self.query_one("#sysprompt-bar", Static)
        bar.update(f"Loading system prompt for {agent_id} …")
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        text = ""
        error = ""
        try:
            from pyclaw.core.prompt_builder import build_system_prompt

            text = build_system_prompt(
                agent_name=self._agent_id,
                config_dir="~/.pyclaw",
            )
        except Exception as e:
            error = f"Error building system prompt: {e}"
        self.app.call_from_thread(self._display, text, error)

    def _display(self, text: str, error: str) -> None:
        bar = self.query_one("#sysprompt-bar", Static)
        content = self.query_one("#sysprompt-text", Static)
        if error:
            bar.update(f"Error — {error[:120]}")
            content.update(error)
        else:
            bar.update(
                f"System prompt for: {self._agent_id}  ({len(text):,} chars)"
            )
            content.update(text)


# ─────────────────────────────── View: Agent Config ──────────────────────────


class AgentConfigView(Vertical):
    """Shows all AgentConfig fields for the active agent."""

    DEFAULT_CSS = """
    AgentConfigView {
        height: 1fr;
        overflow: hidden hidden;
    }
    AgentConfigView #cfg-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    AgentConfigView #cfg-table {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="cfg-bar")
        yield DataTable(id="cfg-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#cfg-table", DataTable)
        t.add_columns("Field", "Value")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load()

    def _load(self) -> None:
        t = self.query_one("#cfg-table", DataTable)
        bar = self.query_one("#cfg-bar", Static)
        t.clear()

        am = getattr(self.gateway, "_agent_manager", None)
        if not am:
            bar.update("Agent manager not available")
            return

        agent = am.agents.get(self._agent_id)
        if not agent:
            bar.update(f"Agent not found: {self._agent_id}")
            return

        cfg = agent.config

        rows: List[Tuple[str, str]] = [
            ("name", cfg.name),
            ("model", cfg.model),
            ("max_tokens", str(cfg.max_tokens)),
            ("temperature", str(cfg.temperature)),
            ("top_p", str(cfg.top_p) if cfg.top_p is not None else "—"),
            ("context_window", f"{cfg.context_window:,}" if cfg.context_window else "—"),
            ("max_iterations", str(cfg.max_iterations) if cfg.max_iterations is not None else "—"),
            ("parallel_tool_calls", str(cfg.parallel_tool_calls) if cfg.parallel_tool_calls is not None else "—"),
            ("streaming_timeout", str(cfg.streaming_timeout) if cfg.streaming_timeout is not None else "—"),
            ("reasoning_effort", cfg.reasoning_effort or "—"),
            ("text_verbosity", cfg.text_verbosity or "—"),
            ("service_tier", cfg.service_tier or "—"),
            ("show_thinking", str(cfg.show_thinking)),
            ("typing_mode", cfg.typing_mode),
            ("use_fastagent", str(cfg.use_fastagent)),
            ("workflow", cfg.workflow or "—"),
        ]

        # Queue config
        q_cfg = getattr(cfg, "queue", None)
        if q_cfg:
            rows.append(("queue.mode", str(getattr(q_cfg, "mode", "—"))))
            rows.append(("queue.debounce_ms", str(getattr(q_cfg, "debounce_ms", "—"))))
            rows.append(("queue.cap", str(getattr(q_cfg, "cap", "—"))))

        # Fallbacks
        fallbacks = getattr(cfg, "fallbacks", [])
        rows.append(("fallbacks", ", ".join(fallbacks) if fallbacks else "—"))

        # Tools profile
        tools_cfg = getattr(cfg, "tools", None)
        if tools_cfg:
            rows.append(("tools.profile", str(getattr(tools_cfg, "profile", "—"))))

        # Extra request_params
        rp = getattr(cfg, "request_params", None)
        if rp:
            for k, v in rp.items():
                rows.append((f"request_params.{k}", str(v)))

        for field_name, value in rows:
            t.add_row(field_name, value, key=field_name)

        bar.update(f"Agent config — {self._agent_id}  ({len(rows)} fields)")


# ─────────────────────────────── View: File Browser ──────────────────────────


class FileBrowserView(Vertical):
    """File browser + viewer/editor for ~/.pyclaw/agents/{id}/"""

    DEFAULT_CSS = """
    FileBrowserView {
        height: 1fr;
        overflow: hidden hidden;
    }
    FileBrowserView #fb-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    FileBrowserView #fb-table {
        height: auto;
        max-height: 50%;
    }
    FileBrowserView #fb-content-bar {
        height: 1;
        background: $panel-darken-2;
        padding: 0 1;
        color: $text-muted;
    }
    FileBrowserView #fb-view {
        height: 1fr;
        overflow-y: scroll;
    }
    FileBrowserView #fb-editor {
        height: 1fr;
        display: none;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""
        self._current_file: Optional[Path] = None
        self._editing: bool = False
        self._agents_dir = Path("~/.pyclaw/agents").expanduser()

    def compose(self) -> ComposeResult:
        yield Static("", id="fb-bar")
        yield DataTable(id="fb-table", zebra_stripes=True, cursor_type="row")
        yield Static("— select a file above —", id="fb-content-bar")
        yield RichLog(id="fb-view", auto_scroll=False, markup=False, highlight=True)
        yield TextArea(id="fb-editor")

    def on_mount(self) -> None:
        t = self.query_one("#fb-table", DataTable)
        t.add_columns("File", "Size", "Modified")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._current_file = None
        self._exit_edit_mode()
        self._load_file_list()

    @work(thread=True)
    def _load_file_list(self) -> None:
        agent_dir = self._agents_dir / self._agent_id
        files: List[Tuple[str, str, str]] = []
        try:
            if agent_dir.exists():
                for p in sorted(agent_dir.rglob("*")):
                    if not p.is_file():
                        continue
                    parts = p.relative_to(agent_dir).parts
                    if any(part in _BROWSER_EXCLUDE_DIRS for part in parts):
                        continue
                    if p.suffix in _BROWSER_EXCLUDE_EXTS:
                        continue
                    rel = str(p.relative_to(agent_dir))
                    size = _fmt_size(p.stat().st_size)
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
                    files.append((rel, size, mtime))
        except Exception as e:
            logger.debug(f"FileBrowserView list error: {e}")
        self.app.call_from_thread(self._populate_list, files)

    def _populate_list(self, files: List[Tuple[str, str, str]]) -> None:
        t = self.query_one("#fb-table", DataTable)
        bar = self.query_one("#fb-bar", Static)
        t.clear()
        for rel, size, mtime in files:
            t.add_row(rel, size, mtime, key=rel)
        bar.update(
            f"{len(files)} files — {self._agent_id}"
            "  \\[Enter=view  e=edit  Ctrl+S=save  Escape=cancel]"
        )

    @on(DataTable.RowSelected, "#fb-table")
    def on_file_selected(self, event: DataTable.RowSelected) -> None:
        rel = str(event.row_key.value)
        self._current_file = self._agents_dir / self._agent_id / rel
        self._exit_edit_mode()
        self._load_file_content()

    @work(thread=True)
    def _load_file_content(self) -> None:
        content = ""
        error = ""
        if self._current_file and self._current_file.exists():
            try:
                content = self._current_file.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                error = str(e)
        self.app.call_from_thread(self._display_content, content, error)

    def _display_content(self, content: str, error: str) -> None:
        view = self.query_one("#fb-view", RichLog)
        bar = self.query_one("#fb-content-bar", Static)
        view.clear()
        if error:
            view.write(f"Error reading file: {error}")
            bar.update(f"Error — {self._current_file}")
        else:
            view.write(content)
            rel = self._rel_path()
            bar.update(f"{rel}  ({len(content):,} chars)  \\[e = edit]")

    def enter_edit_mode(self) -> bool:
        """Switch to TextArea edit mode. Returns True if successful."""
        if not self._current_file or self._editing:
            return False
        try:
            content = self._current_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        self._editing = True
        editor = self.query_one("#fb-editor", TextArea)
        view = self.query_one("#fb-view", RichLog)
        editor.load_text(content)
        view.display = False
        editor.display = True
        editor.focus()
        bar = self.query_one("#fb-content-bar", Static)
        bar.update(f"EDITING: {self._rel_path()}  \\[Ctrl+S=save  Escape=cancel]")
        return True

    def save_file(self) -> bool:
        """Save the TextArea content to disk. Returns True on success."""
        if not self._editing or not self._current_file:
            return False
        editor = self.query_one("#fb-editor", TextArea)
        content = editor.text
        try:
            self._current_file.write_text(content, encoding="utf-8")
            # Tell the file watcher we did this write so it doesn't re-trigger
            fw = getattr(getattr(self.app, "gateway", None), "_file_watcher", None)
            if fw:
                fw.acknowledge(self._current_file)
        except Exception as e:
            bar = self.query_one("#fb-content-bar", Static)
            bar.update(f"Save FAILED: {e}")
            return False
        self._exit_edit_mode()
        # Re-display the saved content
        self._load_file_content()
        return True

    def cancel_edit(self) -> bool:
        """Exit edit mode without saving. Returns True if was editing."""
        if not self._editing:
            return False
        self._exit_edit_mode()
        self._load_file_content()
        return True

    def _exit_edit_mode(self) -> None:
        self._editing = False
        try:
            self.query_one("#fb-editor", TextArea).display = False
            self.query_one("#fb-view", RichLog).display = True
        except Exception:
            pass

    def _rel_path(self) -> str:
        if not self._current_file:
            return ""
        try:
            return str(self._current_file.relative_to(self._agents_dir / self._agent_id))
        except Exception:
            return str(self._current_file)

    def is_editing(self) -> bool:
        return self._editing


# ─────────────────────────────── View: Skills ────────────────────────────────


class SkillsView(Vertical):
    """Lists discovered skills + SKILL.md body viewer."""

    DEFAULT_CSS = """
    SkillsView {
        height: 1fr;
        overflow: hidden hidden;
    }
    SkillsView #sk-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    SkillsView #sk-table {
        height: auto;
        max-height: 50%;
    }
    SkillsView #sk-body-bar {
        height: 1;
        background: $panel-darken-2;
        padding: 0 1;
        color: $text-muted;
    }
    SkillsView #sk-body {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="sk-bar")
        yield DataTable(id="sk-table", zebra_stripes=True, cursor_type="row")
        yield Static("Select a skill to view its SKILL.md", id="sk-body-bar")
        yield RichLog(id="sk-body", auto_scroll=False, markup=False, highlight=True)

    def on_mount(self) -> None:
        t = self.query_one("#sk-table", DataTable)
        t.add_columns("Name", "Version", "Allowed Tools", "Description")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        skills: List[Any] = []
        try:
            from pyclaw.skills.registry import discover_skills

            gw = getattr(self.app, "gateway", None)
            am = getattr(gw, "_agent_manager", None)
            gw_dirs: List[str] = []
            agent_dirs: List[str] = []
            if am:
                agent = am.agents.get(self._agent_id)
                if agent:
                    pc = getattr(agent, "pyclaw_config", None)
                    gw_cfg = getattr(pc, "gateway", None) if pc else None
                    gw_dirs = list(getattr(gw_cfg, "skills_dirs", None) or [])
                    agent_dirs = list(getattr(agent.config, "skills_dirs", None) or [])
                else:
                    logger.warning(
                        f"SkillsView: agent '{self._agent_id}' not found "
                        f"(keys={list(am.agents.keys())})"
                    )
            extra = gw_dirs + agent_dirs
            skills = discover_skills(
                agent_name=self._agent_id,
                config_dir="~/.pyclaw",
                extra_dirs=extra or None,
            )
        except Exception as e:
            logger.warning(f"SkillsView load error: {e}", exc_info=True)
        self.app.call_from_thread(self._populate, skills)

    def _populate(self, skills: List[Any]) -> None:
        t = self.query_one("#sk-table", DataTable)
        bar = self.query_one("#sk-bar", Static)
        t.clear()
        for skill in sorted(skills, key=lambda s: s.name.lower()):
            t.add_row(
                skill.name,
                skill.version or "—",
                " ".join(skill.allowed_tools) if skill.allowed_tools else "—",
                (skill.description or "")[:80],
                key=skill.name,
            )
        bar.update(
            f"{len(skills)} skill{'s' if len(skills) != 1 else ''} — agent: {self._agent_id}"
            "  \\[Enter = view SKILL.md]"
        )

    @on(DataTable.RowSelected, "#sk-table")
    def on_skill_selected(self, event: DataTable.RowSelected) -> None:
        self._load_body(str(event.row_key.value))

    @work(thread=True)
    def _load_body(self, skill_name: str) -> None:
        body = ""
        error = ""
        try:
            from pyclaw.skills.registry import find_skill

            skill = find_skill(skill_name, agent_name=self._agent_id, config_dir="~/.pyclaw")
            if skill:
                body = skill.read_content()
            else:
                error = f"Skill not found: {skill_name}"
        except Exception as e:
            error = str(e)
        self.app.call_from_thread(self._display_body, skill_name, body, error)

    def _display_body(self, skill_name: str, body: str, error: str) -> None:
        log = self.query_one("#sk-body", RichLog)
        bar = self.query_one("#sk-body-bar", Static)
        log.clear()
        if error:
            bar.update(f"Error — {error[:100]}")
            log.write(error)
        else:
            bar.update(f"SKILL.md — {skill_name}  ({len(body):,} chars)")
            log.write(body)


# ─────────────────────────────── View: Run History ───────────────────────────


class RunHistoryView(Vertical):
    """Shows run history for a job from ~/.pyclaw/agents/{id}/runs/ JSONL."""

    DEFAULT_CSS = """
    RunHistoryView {
        height: 1fr;
        overflow: hidden hidden;
    }
    RunHistoryView #rh-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    RunHistoryView #rh-table {
        height: auto;
        max-height: 50%;
    }
    RunHistoryView #rh-detail-bar {
        height: 1;
        background: $panel-darken-2;
        padding: 0 1;
        color: $text-muted;
    }
    RunHistoryView #rh-detail {
        height: 1fr;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""
        self._job_id: str = ""
        self._job_name: str = ""
        self._runs: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "Select a job in Jobs view then press v to load run history.",
            id="rh-bar",
        )
        yield DataTable(id="rh-table", zebra_stripes=True, cursor_type="row")
        yield Static("Select a run to view its output", id="rh-detail-bar")
        yield RichLog(id="rh-detail", auto_scroll=False, markup=False, highlight=False)

    def on_mount(self) -> None:
        t = self.query_one("#rh-table", DataTable)
        t.add_columns("Started", "Duration", "Status", "Output Preview")

    def load_job(self, agent_id: str, job_id: str, job_name: str = "") -> None:
        self._agent_id = agent_id
        self._job_id = job_id
        self._job_name = job_name
        self.query_one("#rh-bar", Static).update(
            f"Loading runs for: {job_name or job_id} …"
        )
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        runs: List[Dict[str, Any]] = []
        runs_file = (
            Path("~/.pyclaw/agents").expanduser()
            / self._agent_id
            / "runs"
            / f"{self._job_id}.jsonl"
        )
        try:
            if runs_file.exists():
                lines = runs_file.read_text().splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if line:
                        try:
                            runs.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            logger.debug(f"RunHistoryView load error: {e}")
        self.app.call_from_thread(self._populate, runs)

    def _populate(self, runs: List[Dict[str, Any]]) -> None:
        self._runs = runs
        t = self.query_one("#rh-table", DataTable)
        bar = self.query_one("#rh-bar", Static)
        t.clear()
        for i, run in enumerate(runs):
            started_raw = run.get("started_at", "")
            started_str = started_raw
            try:
                started_str = datetime.fromisoformat(started_raw).strftime("%m-%d %H:%M:%S")
            except Exception:
                pass
            dur = ""
            try:
                s = datetime.fromisoformat(run.get("started_at", ""))
                e = datetime.fromisoformat(run.get("ended_at", ""))
                dur = f"{(e - s).total_seconds():.1f}s"
            except Exception:
                pass
            status = run.get("status", "?")
            preview = (run.get("stdout", "") or "").replace("\n", " ")[:60]
            t.add_row(started_str, dur, status, preview, key=str(i))
        bar.update(
            f"{len(runs)} run{'s' if len(runs) != 1 else ''} — {self._job_name or self._job_id}"
            "  \\[Enter = view output]"
        )

    @on(DataTable.RowSelected, "#rh-table")
    def on_run_selected(self, event: DataTable.RowSelected) -> None:
        idx = int(str(event.row_key.value))
        if 0 <= idx < len(self._runs):
            run = self._runs[idx]
            log = self.query_one("#rh-detail", RichLog)
            detail_bar = self.query_one("#rh-detail-bar", Static)
            log.clear()
            log.write(f"Run ID:  {run.get('id', '?')}")
            log.write(f"Status:  {run.get('status', '?')}")
            log.write(f"Started: {run.get('started_at', '?')}")
            log.write(f"Ended:   {run.get('ended_at', '?')}")
            if run.get("error"):
                log.write(f"Error:   {run['error']}")
            log.write("")
            log.write("=== STDOUT ===")
            log.write(run.get("stdout", "") or "(empty)")
            if run.get("stderr"):
                log.write("")
                log.write("=== STDERR ===")
                log.write(run["stderr"])
            detail_bar.update(f"Run {run.get('id', '?')}  \\[{run.get('status', '?')}]")


# ─────────────────────────────── View: Agent Log ─────────────────────────────


class AgentLogView(Vertical):
    """Live tail of ~/.pyclaw/agents/{id}/logs/agent.log"""

    DEFAULT_CSS = """
    AgentLogView {
        height: 1fr;
        overflow: hidden hidden;
    }
    AgentLogView #al-bar {
        height: 1;
        background: $panel-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    AgentLogView #al-log {
        height: 1fr;
    }
    """

    TAIL_LINES = 500

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""
        self._log_path: Optional[Path] = None
        self._last_size: int = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="al-bar")
        yield RichLog(id="al-log", auto_scroll=True, markup=False, highlight=False)

    def refresh_for_agent(self, agent_id: str) -> None:
        if self._agent_id == agent_id:
            return
        self._agent_id = agent_id
        self._log_path = (
            Path("~/.pyclaw/agents").expanduser() / agent_id / "logs" / "agent.log"
        )
        self._last_size = 0
        self._do_load()

    def tail_refresh(self) -> None:
        """Append any new log lines since last read."""
        if self._agent_id:
            self._do_tail()

    @work(thread=True)
    def _do_load(self) -> None:
        lines: List[str] = []
        exists = False
        try:
            if self._log_path and self._log_path.exists():
                exists = True
                all_lines = self._log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                lines = all_lines[-self.TAIL_LINES:]
                self._last_size = self._log_path.stat().st_size
        except Exception as e:
            lines = [f"Error reading log: {e}"]
        self.app.call_from_thread(self._populate, lines, exists)

    def _populate(self, lines: List[str], exists: bool) -> None:
        log = self.query_one("#al-log", RichLog)
        bar = self.query_one("#al-bar", Static)
        log.clear()
        if not exists:
            bar.update(f"No agent.log found for: {self._agent_id}")
            log.write(f"Log file not found: {self._log_path}")
            return
        for line in lines:
            log.write(line)
        bar.update(
            f"agent.log — {self._agent_id}  (last {len(lines)} lines, live tail)"
        )

    @work(thread=True)
    def _do_tail(self) -> None:
        new_lines: List[str] = []
        try:
            if self._log_path and self._log_path.exists():
                size = self._log_path.stat().st_size
                if size > self._last_size:
                    with open(self._log_path, encoding="utf-8", errors="replace") as f:
                        f.seek(self._last_size)
                        new_content = f.read()
                    self._last_size = size
                    new_lines = new_content.splitlines()
        except Exception as e:
            logger.debug(f"AgentLogView tail error: {e}")
        if new_lines:
            self.app.call_from_thread(self._append_lines, new_lines)

    def _append_lines(self, lines: List[str]) -> None:
        log = self.query_one("#al-log", RichLog)
        for line in lines:
            log.write(line)


# ─────────────────────────────── View: Agent Card ────────────────────────────


class AgentCardView(Vertical):
    """Summary card for the active agent."""

    DEFAULT_CSS = """
    AgentCardView {
        height: 1fr;
        overflow-y: auto;
    }
    #ac-header {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }
    #ac-content {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._agent_id: str = ""

    def compose(self) -> ComposeResult:
        yield Static("Agent Card", id="ac-header")
        yield RichLog(id="ac-content", markup=True, highlight=False, auto_scroll=False)

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load(agent_id)

    @work(thread=True)
    def _load(self, agent_id: str) -> None:
        lines: List[str] = []
        try:
            am = getattr(self.gateway, "_agent_manager", None)
            agent = am.agents.get(agent_id) if am else None
            if not agent:
                lines.append(f"[dim]No agent: {agent_id}[/dim]")
            else:
                cfg = agent.config

                lines.append(f"[bold cyan]{agent_id}[/bold cyan]")
                lines.append("")

                # Model
                model = getattr(cfg, "model", None) or "(default)"
                lines.append(f"  [bold]Model:[/bold]       {model}")

                # Context window
                ctx = getattr(cfg, "context_window", None)
                if ctx:
                    lines.append(f"  [bold]Context:[/bold]     {ctx:,} tokens")

                # Status
                runner = agent._session_runners.get("__base__")
                status = "ready" if runner else "idle"
                lines.append(f"  [bold]Status:[/bold]      {status}")

                lines.append("")

                # Sessions
                sm = getattr(self.gateway, "_session_manager", None)
                session_count = 0
                msg_count = 0
                if sm:
                    sessions = sm.list_sessions_sync(agent_id=agent_id)
                    session_count = len(sessions)
                    msg_count = sum(
                        getattr(s, "message_count", 0) or 0 for s in sessions
                    )
                lines.append(f"  [bold]Sessions:[/bold]    {session_count}  ({msg_count} messages total)")

                # Jobs
                js = getattr(self.gateway, "_job_scheduler", None)
                job_count = 0
                if js:
                    job_count = sum(
                        1 for j in js.jobs.values()
                        if getattr(j, "run", None) and
                           getattr(j.run, "agent", None) == agent_id
                    )
                lines.append(f"  [bold]Jobs:[/bold]        {job_count} assigned")

                # Skills
                try:
                    from pyclaw.skills.registry import discover_skills
                    extra_dirs = list(getattr(cfg, "skills_dirs", None) or [])
                    gw_cfg = getattr(self.gateway, "_config", None)
                    if gw_cfg and gw_cfg.gateway:
                        for d in (getattr(gw_cfg.gateway, "skills_dirs", None) or []):
                            if d not in extra_dirs:
                                extra_dirs.append(d)
                    skills = discover_skills(
                        agent_name=agent_id,
                        config_dir="~/.pyclaw",
                        extra_dirs=extra_dirs or None,
                    )
                    lines.append(f"  [bold]Skills:[/bold]      {len(skills)}")
                    if skills:
                        for s in sorted(skills, key=lambda x: x.name.lower())[:10]:
                            lines.append(f"    • {s.name}  [dim]v{s.version}[/dim]")
                        if len(skills) > 10:
                            lines.append(f"    … and {len(skills) - 10} more")
                except Exception as e:
                    lines.append(f"  [bold]Skills:[/bold]      (error: {e})")

                lines.append("")

                # Config summary
                lines.append("  [bold]Config:[/bold]")
                for attr in (
                    "description", "prompt_preset", "show_thinking",
                    "typing_mode", "memory_backend",
                ):
                    val = getattr(cfg, attr, None)
                    if val is not None:
                        lines.append(f"    {attr}: {val}")

                # OTel span count
                from pyclaw.core import otel_store
                store = otel_store.get_store()
                if store is not None:
                    lines.append("")
                    lines.append(f"  [bold]OTel spans:[/bold]  {len(store)} buffered")

        except Exception as e:
            lines.append(f"[red]Error loading agent card: {e}[/red]")

        self.app.call_from_thread(self._populate, lines)

    def _populate(self, lines: List[str]) -> None:
        log = self.query_one("#ac-content", RichLog)
        log.clear()
        for line in lines:
            log.write(line)


# ─────────────────────────────── View: Traces ────────────────────────────────


class TracesView(Vertical):
    """Live OpenTelemetry span table from the in-process OTel store."""

    DEFAULT_CSS = """
    TracesView {
        height: 1fr;
        overflow: hidden hidden;
    }
    #tr-bar {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }
    #tr-table {
        height: auto;
        max-height: 50%;
    }
    #tr-detail {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway
        self._spans: List[dict] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "OTel Traces  — \\[Enter] detail  \\[r] refresh",
            id="tr-bar",
        )
        t = DataTable(id="tr-table", cursor_type="row")
        t.add_columns("Time", "Span", "Model", "In", "Out", "Dur", "Status")
        yield t
        yield RichLog(id="tr-detail", markup=True, highlight=False, auto_scroll=False)

    def on_mount(self) -> None:
        self._load_spans()

    def refresh_for_agent(self, agent_id: str) -> None:
        self._load_spans()

    @work(thread=True)
    def _load_spans(self) -> None:
        from pyclaw.core import otel_store
        store = otel_store.get_store()
        if store is None:
            self.app.call_from_thread(self._populate, [])
            return
        raw = store.recent(200)
        summaries = [otel_store.span_summary(s) for s in reversed(raw)]
        self.app.call_from_thread(self._populate, summaries)

    def _populate(self, summaries: List[dict]) -> None:
        self._spans = summaries
        t = self.query_one("#tr-table", DataTable)
        t.clear()
        for i, s in enumerate(summaries):
            name = s.get("name", "?")
            if len(name) > 40:
                name = name[:37] + "…"
            t.add_row(
                s.get("ts", "?"),
                name,
                s.get("model", "") or "—",
                s.get("in_toks", "—"),
                s.get("out_toks", "—"),
                s.get("dur", "?"),
                s.get("status", "?"),
                key=str(i),
            )
        detail = self.query_one("#tr-detail", RichLog)
        detail.clear()
        count = len(summaries)
        from pyclaw.core import otel_store
        store = otel_store.get_store()
        buffered = len(store) if store is not None else 0
        detail.write(f"[dim]{count} spans shown  ({buffered} buffered total) — select a row to inspect[/dim]")

    @on(DataTable.RowSelected, "#tr-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = int(str(event.row_key.value))
            span = self._spans[idx]
        except (ValueError, IndexError):
            return
        detail = self.query_one("#tr-detail", RichLog)
        detail.clear()
        detail.write(f"[bold]{span.get('name', '?')}[/bold]  [{span.get('status', '?')}]")
        detail.write(f"  Time:     {span.get('ts', '?')}   Duration: {span.get('dur', '?')}")
        detail.write(f"  Trace ID: {span.get('trace_id', '')}")
        detail.write(f"  Span ID:  {span.get('span_id', '')}")
        model = span.get("model", "")
        if model:
            detail.write(f"  Model:    {model}")
        in_t = span.get("in_toks", "—")
        out_t = span.get("out_toks", "—")
        if in_t != "—" or out_t != "—":
            detail.write(f"  Tokens:   in={in_t}  out={out_t}")
        attrs = span.get("attrs", {})
        if attrs:
            detail.write("")
            detail.write("[bold]Attributes:[/bold]")
            for k, v in sorted(attrs.items()):
                val_str = str(v)
                if len(val_str) > 120:
                    val_str = val_str[:117] + "…"
                detail.write(f"  {k}: {val_str}")


# ─────────────────────────────── Main Dashboard ──────────────────────────────


class GatewayDashboard(App):
    """Unified pyclaw gateway dashboard."""

    TITLE = "PyClaw Gateway"
    SUB_TITLE = "Dashboard"

    CSS = """
    Screen {
        background: $surface;
    }

    #agent-tabs {
        height: 3;
        background: $panel;
    }

    #view-tabs {
        height: 3;
        background: $panel-darken-1;
    }

    #status-bar {
        height: 1;
        background: $panel-darken-2;
        padding: 0 1;
        color: $text-muted;
    }

    #split-area {
        height: 1fr;
        overflow: hidden hidden;
    }

    #detail-pane {
        height: 7fr;
        border-bottom: solid $border;
        overflow: hidden hidden;
    }

    #log-pane {
        height: 3fr;
        overflow: hidden hidden;
    }

    #log-header {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }

    #log-richlog {
        height: 1fr;
        overflow-y: scroll;
        overflow-x: hidden;
    }

    ContentSwitcher {
        height: 1fr;
        overflow: hidden hidden;
    }
    """

    BINDINGS = [
        Binding("0", "view_agentcard", "0:Card", show=True),
        Binding("1", "view_sessions", "1:Sessions", show=True),
        Binding("2", "view_history", "2:History", show=True),
        Binding("3", "view_jobs", "3:Jobs", show=True),
        Binding("4", "view_sysprompt", "4:SysPrompt", show=True),
        Binding("5", "view_config", "5:Config", show=True),
        Binding("6", "view_files", "6:Files", show=True),
        Binding("7", "view_skills", "7:Skills", show=True),
        Binding("8", "view_runhistory", "8:RunHist", show=True),
        Binding("9", "view_agentlog", "9:AgentLog", show=True),
        Binding("t", "view_traces", "t:Traces", show=True),
        Binding("h", "load_history", "h:Hist", show=True),
        Binding("r", "run_job", "r:Run", show=True),
        Binding("v", "view_job_runs", "v:Runs", show=True),
        Binding("e", "edit_file", "e:Edit", show=False),
        Binding("ctrl+s", "save_file", "Ctrl+S:Save", show=False, priority=True),
        Binding("escape", "cancel_edit", "Esc:Cancel", show=False, priority=True),
        Binding("[", "shrink_log", "[:Shrink", show=True),
        Binding("]", "grow_log", "]:Grow", show=True),
        Binding("y", "yank", "y:Copy", show=True),
        Binding("f5", "refresh_view", "F5:Refresh", show=False),
        Binding("q", "quit", "q:Quit", show=True),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    _log_pct: reactive[int] = reactive(30)
    _active_agent: reactive[str] = reactive("")
    _active_view: reactive[str] = reactive("agentcard")

    def __init__(self, gateway: Any = None) -> None:
        super().__init__()
        self.gateway = gateway
        self._log_handler: Optional[_QueueLogHandler] = None
        self._suppressed_handlers: list[tuple[logging.Logger, logging.Handler]] = []

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Tabs(id="agent-tabs")
        yield Tabs(
            Tab("Card", id="tab-agentcard"),
            Tab("Sessions", id="tab-sessions"),
            Tab("History", id="tab-history"),
            Tab("Jobs", id="tab-jobs"),
            Tab("Sys Prompt", id="tab-sysprompt"),
            Tab("Config", id="tab-config"),
            Tab("Files", id="tab-files"),
            Tab("Skills", id="tab-skills"),
            Tab("Run Hist", id="tab-runhistory"),
            Tab("Agent Log", id="tab-agentlog"),
            Tab("Traces", id="tab-traces"),
            id="view-tabs",
        )
        yield Static("", id="status-bar")

        with Vertical(id="split-area"):
            with ContentSwitcher(id="detail-pane", initial="view-agentcard"):
                yield AgentCardView(self.gateway, id="view-agentcard")
                yield SessionsView(self.gateway, id="view-sessions")
                yield HistoryView(self.gateway, id="view-history")
                yield JobsView(self.gateway, id="view-jobs")
                yield SystemPromptView(self.gateway, id="view-sysprompt")
                yield AgentConfigView(self.gateway, id="view-config")
                yield FileBrowserView(self.gateway, id="view-files")
                yield SkillsView(self.gateway, id="view-skills")
                yield RunHistoryView(self.gateway, id="view-runhistory")
                yield AgentLogView(self.gateway, id="view-agentlog")
                yield TracesView(self.gateway, id="view-traces")

            with Vertical(id="log-pane"):
                yield Static(
                    "Gateway Logs  — \\[ / ] to resize",
                    id="log-header",
                )
                yield RichLog(
                    id="log-richlog",
                    auto_scroll=True,
                    markup=False,
                    highlight=False,
                )

        yield Footer()

    # ── Mount / Unmount ───────────────────────────────────────────────────────

    def on_mount(self) -> None:
        root = logging.getLogger()
        _sweep_targets = [
            root,
            logging.getLogger("uvicorn"),
            logging.getLogger("uvicorn.access"),
            logging.getLogger("uvicorn.error"),
            logging.getLogger("fastapi"),
        ]
        self._suppressed_handlers = []
        for target_logger in _sweep_targets:
            for h in list(target_logger.handlers):
                if (
                    isinstance(h, logging.StreamHandler)
                    and not isinstance(h, logging.FileHandler)
                    and not isinstance(h, _QueueLogHandler)
                ):
                    target_logger.removeHandler(h)
                    self._suppressed_handlers.append((target_logger, h))

        fmt = logging.Formatter(
            "%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )
        self._log_handler = _QueueLogHandler(_LOG_QUEUE)
        self._log_handler.setFormatter(fmt)
        self._log_handler.setLevel(logging.INFO)
        root.addHandler(self._log_handler)

        self._populate_agent_tabs()

        self.set_interval(0.2, self._drain_logs)
        self.set_interval(5.0, self._auto_refresh)
        self.set_interval(2.0, self._tail_agent_log)

    def on_unmount(self) -> None:
        root = logging.getLogger()
        if self._log_handler:
            root.removeHandler(self._log_handler)
            self._log_handler = None
        for target_logger, h in self._suppressed_handlers:
            target_logger.addHandler(h)
        self._suppressed_handlers.clear()

    # ── Agent tab strip ───────────────────────────────────────────────────────

    def _populate_agent_tabs(self) -> None:
        agent_tabs = self.query_one("#agent-tabs", Tabs)
        agents: List[str] = []
        am = getattr(self.gateway, "_agent_manager", None)
        if am:
            agents = list(am.agents.keys())
        if not agents:
            agents = ["(no agents)"]
        for agent_id in agents:
            agent_tabs.add_tab(Tab(agent_id, id=f"agent-{agent_id}"))
        if agents:
            self._active_agent = agents[0]

    @on(Tabs.TabActivated, "#agent-tabs")
    def on_agent_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        if tab_id.startswith("agent-"):
            agent_id = tab_id[len("agent-"):]
            self._active_agent = agent_id
            self._refresh_view_for_agent(agent_id)

    # ── View tab strip ────────────────────────────────────────────────────────

    @on(Tabs.TabActivated, "#view-tabs")
    def on_view_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_map = {
            "tab-agentcard": "agentcard",
            "tab-sessions": "sessions",
            "tab-history": "history",
            "tab-jobs": "jobs",
            "tab-sysprompt": "sysprompt",
            "tab-config": "config",
            "tab-files": "files",
            "tab-skills": "skills",
            "tab-runhistory": "runhistory",
            "tab-agentlog": "agentlog",
            "tab-traces": "traces",
        }
        view = tab_map.get(event.tab.id or "")
        if view:
            self._switch_view(view)

    # ── View switching ────────────────────────────────────────────────────────

    def _switch_view(self, view: str) -> None:
        self._active_view = view
        switcher = self.query_one("#detail-pane", ContentSwitcher)
        switcher.current = f"view-{view}"
        self._refresh_view_for_agent(self._active_agent)

    def _set_view_tab(self, tab_id: str) -> None:
        self.query_one("#view-tabs", Tabs).active = tab_id

    def _refresh_view_for_agent(self, agent_id: str) -> None:
        if not agent_id or agent_id == "(no agents)":
            return
        view = self._active_view
        if view == "agentcard":
            self.query_one(AgentCardView).refresh_for_agent(agent_id)
        elif view == "sessions":
            self.query_one(SessionsView).refresh_for_agent(agent_id)
        elif view == "jobs":
            self.query_one(JobsView).refresh_for_agent(agent_id)
        elif view == "sysprompt":
            self.query_one(SystemPromptView).refresh_for_agent(agent_id)
        elif view == "config":
            self.query_one(AgentConfigView).refresh_for_agent(agent_id)
        elif view == "files":
            self.query_one(FileBrowserView).refresh_for_agent(agent_id)
        elif view == "skills":
            self.query_one(SkillsView).refresh_for_agent(agent_id)
        elif view == "agentlog":
            self.query_one(AgentLogView).refresh_for_agent(agent_id)
        elif view == "traces":
            self.query_one(TracesView).refresh_for_agent(agent_id)
        # history and runhistory are loaded on demand

    def _auto_refresh(self) -> None:
        if self._active_view in ("sessions", "jobs", "agentcard", "traces"):
            self._refresh_view_for_agent(self._active_agent)
        self._update_status_bar()

    def _tail_agent_log(self) -> None:
        if self._active_view == "agentlog":
            self.query_one(AgentLogView).tail_refresh()

    # ── Status bar ────────────────────────────────────────────────────────────

    def _update_status_bar(self) -> None:
        bar = self.query_one("#status-bar", Static)
        parts: List[str] = []

        usage = getattr(self.gateway, "_usage", {}) or {}

        started_at = usage.get("started_at")
        if started_at:
            try:
                now_utc = datetime.now(timezone.utc)
                if isinstance(started_at, datetime):
                    sa = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
                else:
                    sa = datetime.fromisoformat(str(started_at))
                    if sa.tzinfo is None:
                        sa = sa.replace(tzinfo=timezone.utc)
                total_secs = int((now_utc - sa).total_seconds())
                h, rem = divmod(max(0, total_secs), 3600)
                m, s = divmod(rem, 60)
                parts.append(f"Up: {h}h{m:02d}m{s:02d}s")
            except Exception:
                pass

        msgs_total = usage.get("messages_total") or usage.get("messages", 0)
        if msgs_total:
            parts.append(f"Msgs: {msgs_total}")

        msgs_by_agent = usage.get("messages_by_agent", {}) or {}
        if self._active_agent and msgs_by_agent.get(self._active_agent):
            parts.append(f"{self._active_agent}: {msgs_by_agent[self._active_agent]}")

        js = getattr(self.gateway, "_job_scheduler", None)
        if js:
            running = getattr(js, "_running_jobs", set())
            total_jobs = len(js.jobs)
            if running:
                parts.append(f"Jobs: {len(running)} running / {total_jobs}")
            else:
                parts.append(f"Jobs: {total_jobs} scheduled")

        bar.update("  |  ".join(parts) if parts else "Gateway active")

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_view_agentcard(self) -> None:
        self._switch_view("agentcard")
        self._set_view_tab("tab-agentcard")

    def action_view_sessions(self) -> None:
        self._switch_view("sessions")
        self._set_view_tab("tab-sessions")

    def action_view_history(self) -> None:
        self._switch_view("history")
        self._set_view_tab("tab-history")

    def action_view_jobs(self) -> None:
        self._switch_view("jobs")
        self._set_view_tab("tab-jobs")

    def action_view_sysprompt(self) -> None:
        self._switch_view("sysprompt")
        self._set_view_tab("tab-sysprompt")

    def action_view_config(self) -> None:
        self._switch_view("config")
        self._set_view_tab("tab-config")

    def action_view_files(self) -> None:
        self._switch_view("files")
        self._set_view_tab("tab-files")

    def action_view_skills(self) -> None:
        self._switch_view("skills")
        self._set_view_tab("tab-skills")

    def action_view_runhistory(self) -> None:
        self._switch_view("runhistory")
        self._set_view_tab("tab-runhistory")

    def action_view_agentlog(self) -> None:
        self._switch_view("agentlog")
        self._set_view_tab("tab-agentlog")

    def action_view_traces(self) -> None:
        self._switch_view("traces")
        self._set_view_tab("tab-traces")

    def action_load_history(self) -> None:
        session_id = self.query_one(SessionsView).get_selected_session_id()
        if not session_id:
            return
        self.action_view_history()
        self.query_one(HistoryView).load_session(self._active_agent, session_id)

    async def action_run_job(self) -> None:
        if self._active_view != "jobs":
            self.action_view_jobs()
            return
        job_id = self.query_one(JobsView).get_selected_job_id()
        log = self.query_one("#log-richlog", RichLog)
        if not job_id:
            log.write("[dashboard] No job selected")
            return
        js = getattr(self.gateway, "_job_scheduler", None)
        if not js:
            log.write("[dashboard] Job scheduler not available")
            return
        job = js.jobs.get(job_id)
        name = (job.name or job_id) if job else job_id
        log.write(f"[dashboard] Triggering job: {name}")
        try:
            await js.run_job_now(job_id)
            log.write(f"[dashboard] Job triggered: {name}")
        except Exception as e:
            log.write(f"[dashboard] Error triggering job: {e}")

    def action_view_job_runs(self) -> None:
        job_id = self.query_one(JobsView).get_selected_job_id()
        log = self.query_one("#log-richlog", RichLog)
        if not job_id:
            log.write("[dashboard] Select a job in Jobs view first (press 3)")
            return
        js = getattr(self.gateway, "_job_scheduler", None)
        job_name = ""
        if js:
            job = js.jobs.get(job_id)
            job_name = (job.name or job_id) if job else job_id
        self.action_view_runhistory()
        self.query_one(RunHistoryView).load_job(self._active_agent, job_id, job_name)

    def action_edit_file(self) -> None:
        if self._active_view != "files":
            return
        fb = self.query_one(FileBrowserView)
        if not fb.enter_edit_mode():
            log = self.query_one("#log-richlog", RichLog)
            log.write("[dashboard] Select a file first (press Enter on a file row)")

    def action_save_file(self) -> None:
        if self._active_view != "files":
            return
        fb = self.query_one(FileBrowserView)
        log = self.query_one("#log-richlog", RichLog)
        if fb.save_file():
            log.write(f"[dashboard] Saved: {fb._rel_path()}")
        elif fb.is_editing():
            log.write("[dashboard] Save failed — check log for details")

    def action_cancel_edit(self) -> None:
        if self._active_view != "files":
            return
        self.query_one(FileBrowserView).cancel_edit()

    def action_refresh_view(self) -> None:
        self._refresh_view_for_agent(self._active_agent)

    def action_shrink_log(self) -> None:
        self._set_log_pct(max(10, self._log_pct - 5))

    def action_grow_log(self) -> None:
        self._set_log_pct(min(80, self._log_pct + 5))

    def _set_log_pct(self, pct: int) -> None:
        self._log_pct = pct
        detail_fr = 100 - pct
        self.query_one("#detail-pane").styles.height = f"{detail_fr}fr"
        self.query_one("#log-pane").styles.height = f"{pct}fr"

    def action_yank(self) -> None:
        """Copy current view content to system clipboard via pbcopy/xclip."""
        import subprocess
        import sys

        text = self._get_yank_text()
        if not text:
            self.query_one("#log-richlog", RichLog).write(
                "[dashboard] Nothing to copy in current view"
            )
            return

        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
            else:
                # Try xclip, fall back to xsel
                try:
                    subprocess.run(
                        ["xclip", "-selection", "clipboard"],
                        input=text.encode(), check=True,
                    )
                except FileNotFoundError:
                    subprocess.run(
                        ["xsel", "--clipboard", "--input"],
                        input=text.encode(), check=True,
                    )
            lines = text.count("\n") + 1
            self.query_one("#log-richlog", RichLog).write(
                f"[dashboard] Copied {lines} lines to clipboard"
            )
        except Exception as e:
            self.query_one("#log-richlog", RichLog).write(
                f"[dashboard] Copy failed: {e}"
            )

    def _get_yank_text(self) -> str:
        """Return text content for the current view to copy to clipboard."""
        view = self._active_view
        agent = self._active_agent
        try:
            if view == "history":
                hv = self.query_one(HistoryView)
                sm = getattr(self.gateway, "_session_manager", None)
                if sm and hv._session_id:
                    sessions = sm.list_sessions_sync(agent_id=hv._agent_id)
                    s = next((x for x in sessions if x.id == hv._session_id), None)
                    if s and s.history_path and s.history_path.exists():
                        return s.history_path.read_text()
            elif view == "sysprompt":
                from pyclaw.core.prompt_builder import build_system_prompt
                return build_system_prompt(agent_name=agent, config_dir="~/.pyclaw")
            elif view == "files":
                fb = self.query_one(FileBrowserView)
                if fb._current_file and fb._current_file.exists():
                    return fb._current_file.read_text(encoding="utf-8", errors="replace")
            elif view == "skills":
                sv = self.query_one(SkillsView)
                t = sv.query_one("#sk-table", DataTable)
                try:
                    skill_name = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
                    from pyclaw.skills.registry import find_skill
                    skill = find_skill(skill_name, agent_name=agent, config_dir="~/.pyclaw")
                    if skill:
                        return skill.read_content()
                except Exception:
                    pass
            elif view == "agentlog":
                log_path = (
                    Path("~/.pyclaw/agents").expanduser() / agent / "logs" / "agent.log"
                )
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    return "\n".join(lines[-500:])
            elif view == "runhistory":
                rh = self.query_one(RunHistoryView)
                t = rh.query_one("#rh-table", DataTable)
                try:
                    idx = int(str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value))
                    if 0 <= idx < len(rh._runs):
                        run = rh._runs[idx]
                        parts = [
                            f"Run: {run.get('id', '?')}",
                            f"Status: {run.get('status', '?')}",
                            f"Started: {run.get('started_at', '?')}",
                            f"Ended: {run.get('ended_at', '?')}",
                            "",
                            "=== STDOUT ===",
                            run.get("stdout", "") or "",
                        ]
                        if run.get("stderr"):
                            parts += ["", "=== STDERR ===", run["stderr"]]
                        return "\n".join(parts)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"yank error: {e}")
        return ""

    def action_quit(self) -> None:
        self.exit()

    # ── Log drain ─────────────────────────────────────────────────────────────

    def _drain_logs(self) -> None:
        log_widget = self.query_one("#log-richlog", RichLog)
        drained = 0
        while drained < 100:
            try:
                msg = _LOG_QUEUE.get_nowait()
                log_widget.write(msg)
                drained += 1
            except queue.Empty:
                break


# ─────────────────────────────── Entry Point ─────────────────────────────────


async def run_dashboard(gateway: Any = None) -> None:
    """Run the gateway dashboard TUI."""
    app = GatewayDashboard(gateway=gateway)
    await app.run_async()
