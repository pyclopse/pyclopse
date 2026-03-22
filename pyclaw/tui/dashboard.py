"""Gateway Dashboard TUI for pyclaw.

Three-pane layout:
  ┌────────────────────────────────────────────────────────┐
  │  Header (title + clock)                                │
  │  [Agent1] [Agent2] [Agent3]  ← agent tab strip        │
  │  [Sessions] [History] [Jobs] [Sys Prompt]  ← view tabs │
  ├────────────────────────────────────────────────────────┤
  │                                                        │
  │  Detail pane  (resizable)                              │
  │                                                        │
  ├────────────────────────────────────────────────────────┤
  │  Log pane — live gateway logs  (resizable)             │
  └────────────────────────────────────────────────────────┘

Key bindings:
  1  Sessions      2  History     3  Jobs     4  Sys Prompt
  h  Load history for selected session
  r  Run selected job now
  [  Shrink log pane    ]  Grow log pane
  F5 Refresh current view
  q  Quit
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import traceback
from typing import Any, List, Optional

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
)

logger = logging.getLogger(__name__)

# ─────────────────────────────── Log Handler ─────────────────────────────────

_LOG_QUEUE: "queue.SimpleQueue[str]" = queue.SimpleQueue()


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
        t.add_columns("Session ID", "Channel", "User", "Msgs", "Updated", "Active")

    def refresh_for_agent(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        sessions: List[Any] = []
        try:
            sm = getattr(self.gateway, "_session_manager", None)
            if sm and self._agent_id:
                sessions = sm.list_sessions_sync(agent_id=self._agent_id)
        except Exception as e:
            logger.debug(f"SessionsView load error: {e}")
        self.app.call_from_thread(self._populate, sessions)

    def _populate(self, sessions: List[Any]) -> None:
        t = self.query_one("#sessions-table", DataTable)
        hint = self.query_one("#sessions-hint", Static)
        t.clear()
        for s in sessions:
            updated = s.updated_at.strftime("%m-%d %H:%M") if s.updated_at else ""
            active = "yes" if getattr(s, "is_active", False) else "no"
            t.add_row(
                s.id,
                s.channel or "",
                str(s.user_id or ""),
                str(s.message_count),
                updated,
                active,
                key=s.id,
            )
        count = len(sessions)
        hint.update(
            f"{count} session{'s' if count != 1 else ''} — agent: {self._agent_id}"
            "  [h = load history for selected]"
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
                f"({len(content):,} chars)  [scroll with arrow keys / page up/down]"
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
        t.add_columns("Name", "Schedule", "Enabled", "Status", "Next Run", "Last Run")

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
            # Describe schedule
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

            t.add_row(
                job.name or job.id,
                schedule_str,
                enabled,
                status,
                next_run,
                last_run,
                key=job.id,
            )

        count = len(agent_jobs)
        bar.update(
            f"{count} job{'s' if count != 1 else ''} — agent: {self._agent_id}"
            "  [r = run selected job now]"
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


# ─────────────────────────────── Main Dashboard ──────────────────────────────


class GatewayDashboard(App):
    """Unified pyclaw gateway dashboard."""

    TITLE = "PyClaw Gateway"
    SUB_TITLE = "Dashboard"

    CSS = """
    Screen {
        background: $surface;
    }

    /* Agent tab strip */
    #agent-tabs {
        height: 3;
        background: $panel;
    }

    /* View selector tabs */
    #view-tabs {
        height: 3;
        background: $panel-darken-1;
    }

    /* Vertical split area fills all remaining space */
    #split-area {
        height: 1fr;
        overflow: hidden hidden;
    }

    /* Detail pane — 7/10 of split area by default */
    #detail-pane {
        height: 7fr;
        border-bottom: solid $border;
        overflow: hidden hidden;
    }

    /* Log pane — 3/10 of split area by default */
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
        Binding("1", "view_sessions", "1:Sessions", show=True),
        Binding("2", "view_history", "2:History", show=True),
        Binding("3", "view_jobs", "3:Jobs", show=True),
        Binding("4", "view_sysprompt", "4:SysPrompt", show=True),
        Binding("h", "load_history", "h:Load Hist", show=True),
        Binding("r", "run_job", "r:Run Job", show=True),
        Binding("[", "shrink_log", "[:Shrink Log", show=True),
        Binding("]", "grow_log", "]:Grow Log", show=True),
        Binding("f5", "refresh_view", "F5:Refresh", show=False),
        Binding("q", "quit", "q:Quit", show=True),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    # Reactive state
    _log_pct: reactive[int] = reactive(30)
    _active_agent: reactive[str] = reactive("")
    _active_view: reactive[str] = reactive("sessions")

    def __init__(self, gateway: Any = None) -> None:
        super().__init__()
        self.gateway = gateway
        self._log_handler: Optional[_QueueLogHandler] = None
        self._suppressed_handlers: list[tuple[logging.Logger, logging.Handler]] = []

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # Agent tab strip — tabs added dynamically in on_mount
        yield Tabs(id="agent-tabs")

        # View selector tabs
        yield Tabs(
            Tab("Sessions", id="tab-sessions"),
            Tab("History", id="tab-history"),
            Tab("Jobs", id="tab-jobs"),
            Tab("Sys Prompt", id="tab-sysprompt"),
            id="view-tabs",
        )

        # Split: detail pane (top) + log pane (bottom)
        with Vertical(id="split-area"):
            with ContentSwitcher(id="detail-pane", initial="view-sessions"):
                yield SessionsView(self.gateway, id="view-sessions")
                yield HistoryView(self.gateway, id="view-history")
                yield JobsView(self.gateway, id="view-jobs")
                yield SystemPromptView(self.gateway, id="view-sysprompt")

            with Vertical(id="log-pane"):
                yield Static(
                    "Gateway Logs  — [ / ] to resize pane",
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

        # Remove StreamHandlers from root logger AND from uvicorn's own loggers
        # (uvicorn installs handlers directly on its loggers, bypassing root).
        # Any handler that writes to a stream (stdout/stderr) will bleed through
        # the Textual display; file handlers and our queue handler are fine.
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

        # Attach queue-based log handler — all logs flow here instead

        # Attach queue-based log handler — all logs flow here instead
        fmt = logging.Formatter(
            "%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )
        self._log_handler = _QueueLogHandler(_LOG_QUEUE)
        self._log_handler.setFormatter(fmt)
        self._log_handler.setLevel(logging.INFO)
        root.addHandler(self._log_handler)

        # Build agent tabs
        self._populate_agent_tabs()

        # Periodic tasks
        self.set_interval(0.2, self._drain_logs)
        self.set_interval(5.0, self._auto_refresh)

    def on_unmount(self) -> None:
        root = logging.getLogger()
        if self._log_handler:
            root.removeHandler(self._log_handler)
            self._log_handler = None
        # Restore suppressed console handlers to their original loggers
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
            # Select first agent; Tabs fires TabActivated which triggers refresh
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
            "tab-sessions": "sessions",
            "tab-history": "history",
            "tab-jobs": "jobs",
            "tab-sysprompt": "sysprompt",
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
        view_tabs = self.query_one("#view-tabs", Tabs)
        view_tabs.active = tab_id

    def _refresh_view_for_agent(self, agent_id: str) -> None:
        if not agent_id or agent_id == "(no agents)":
            return
        view = self._active_view
        if view == "sessions":
            self.query_one(SessionsView).refresh_for_agent(agent_id)
        elif view == "jobs":
            self.query_one(JobsView).refresh_for_agent(agent_id)
        elif view == "sysprompt":
            self.query_one(SystemPromptView).refresh_for_agent(agent_id)
        # History is loaded on demand (h key), not auto-refreshed

    def _auto_refresh(self) -> None:
        """Periodic refresh for live-data views."""
        if self._active_view in ("sessions", "jobs"):
            self._refresh_view_for_agent(self._active_agent)

    # ── Actions ───────────────────────────────────────────────────────────────

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

    def action_load_history(self) -> None:
        """Load history for the session currently selected in Sessions view."""
        sessions_view = self.query_one(SessionsView)
        session_id = sessions_view.get_selected_session_id()
        if not session_id:
            return
        self.action_view_history()
        self.query_one(HistoryView).load_session(self._active_agent, session_id)

    async def action_run_job(self) -> None:
        """Run the job currently selected in the Jobs view."""
        if self._active_view != "jobs":
            self.action_view_jobs()
            return
        jobs_view = self.query_one(JobsView)
        job_id = jobs_view.get_selected_job_id()
        log = self.query_one("#log-richlog", RichLog)
        if not job_id:
            log.write("[dashboard] No job selected — navigate to Jobs view and select a row")
            return
        js = getattr(self.gateway, "_job_scheduler", None)
        if not js:
            log.write("[dashboard] Job scheduler not available")
            return
        job = js.jobs.get(job_id)
        name = (job.name or job_id) if job else job_id
        log.write(f"[dashboard] Triggering job: {name} ({job_id})")
        try:
            await js.run_job_now(job_id)
            log.write(f"[dashboard] Job triggered: {name}")
        except Exception as e:
            log.write(f"[dashboard] Error triggering job: {e}")

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
