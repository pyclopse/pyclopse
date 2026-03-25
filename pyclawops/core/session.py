"""Session management for pyclawops."""

import asyncio
from pyclawops.reflect import reflect_system
import json
import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclawops.core.router import IncomingMessage, OutgoingMessage
from pyclawops.utils.time import now, today_midnight

# Default agents directory
_DEFAULT_AGENTS_DIR = "~/.pyclawops/agents"

_SESSION_ALPHABET = string.ascii_letters + string.digits


def _generate_session_id() -> str:
    """Generate a date-prefixed session ID in the form YYYY-MM-DD-XXXXXX.

    The suffix is 6 random alphanumeric characters chosen from
    ``string.ascii_letters + string.digits`` using the secrets module for
    cryptographic randomness.

    Returns:
        str: Session ID string, e.g. ``"2025-03-23-aB3xQz"``.
    """
    suffix = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(6))
    return f"{now().strftime('%Y-%m-%d')}-{suffix}"


@reflect_system("sessions")
@dataclass
class Session:
    """A conversation session — metadata only; history lives in files on disk.

    The session object is a lightweight metadata container.  Actual message
    history is stored as FA-native JSON in ``history_dir/history.json`` and
    managed by AgentRunner.  This separation allows the reaper to evict
    sessions from the in-memory index without touching history files, and
    allows sessions to resume from disk after a gateway restart.

    Attributes:
        id (str): Unique session identifier (YYYY-MM-DD-XXXXXX format).
        agent_id (str): The agent that owns this session.
        channel (str): Originating channel (e.g. "telegram", "slack", "tui").
        user_id (str): Stable user identifier on the originating channel.
        created_at (datetime): Session creation timestamp.
        updated_at (datetime): Timestamp of last activity (touch() updates this).
        metadata (Dict[str, Any]): Arbitrary metadata (e.g. Telegram chat info).
        context (Dict[str, Any]): Runtime context dict (model_override,
            instruction_override, show_thinking, etc.).
        is_active (bool): Whether the session is active in the index.
        message_count (int): Number of messages processed (user + assistant
            turns together count as 2).
        history_dir (Optional[Path]): Absolute path to the session directory on
            disk; None for ephemeral sessions.
        last_channel (Optional[str]): Channel of the most recent message
            (may differ from channel for multi-channel sessions).
        last_user_id (Optional[str]): User ID of the most recent sender.
        last_thread_ts (Optional[str]): Slack thread_ts of the most recent
            message, if applicable.
    """

    id: str
    agent_id: str
    channel: str
    user_id: str
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    message_count: int = 0
    # Absolute path to the session directory (set by SessionManager)
    history_dir: Optional[Path] = field(default=None, repr=False, compare=False)
    # Routing fields: updated each message so replies go back to the right place
    last_channel: Optional[str] = field(default=None)
    last_user_id: Optional[str] = field(default=None)
    last_thread_ts: Optional[str] = field(default=None)

    @property
    def history_path(self) -> Optional[Path]:
        """Return the path to the primary FA-native history JSON file.

        Returns:
            Optional[Path]: ``history_dir/history.json``, or None if this is
                an ephemeral session with no history_dir.
        """
        if self.history_dir is None:
            return None
        return self.history_dir / "history.json"

    def touch(self, count_delta: int = 0) -> None:
        """Update the last-activity timestamp and optionally increment message_count.

        Called after each successful message exchange to keep the session
        fresh in the reaper's TTL window and persist the updated metadata.

        Args:
            count_delta (int): Amount to add to message_count. Defaults to 0.
        """
        self.updated_at = now()
        self.message_count += count_delta
        self.save_metadata()

    def save_metadata(self) -> None:
        """Persist session metadata to disk using an atomic write.

        Writes to ``history_dir/session.json`` via a temporary file and
        atomic rename.  Silently returns if no history_dir is set (ephemeral
        sessions).  Logs errors but never raises.
        """
        if self.history_dir is None:
            return
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            path = self.history_dir / "session.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2))
            tmp.replace(path)
        except Exception as e:
            logging.getLogger("pyclawops.session").error(
                f"Failed to save session metadata {self.id}: {e}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize session metadata to a JSON-compatible dict.

        Excludes message content (history lives in history.json) and strips
        private context keys (those starting with ``_``).

        Returns:
            Dict[str, Any]: Serializable dict suitable for writing to
                session.json.
        """
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "channel": self.channel,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "message_count": self.message_count,
            "is_active": self.is_active,
            "metadata": self.metadata,
            "context": {
                k: v for k, v in self.context.items()
                if not k.startswith("_")
            },
            "last_channel": self.last_channel,
            "last_user_id": self.last_user_id,
            "last_thread_ts": self.last_thread_ts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], history_dir: Optional[Path] = None) -> "Session":
        """Restore a Session from a persisted metadata dictionary.

        Args:
            data (Dict[str, Any]): Dict as written by to_dict() / session.json.
            history_dir (Optional[Path]): Absolute path to the session directory
                on disk.  Defaults to None (ephemeral).

        Returns:
            Session: Reconstructed Session instance.
        """
        return cls(
            id=data["id"],
            agent_id=data["agent_id"],
            channel=data["channel"],
            user_id=data["user_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            message_count=data.get("message_count", 0),
            is_active=data.get("is_active", True),
            metadata=data.get("metadata", {}),
            context=data.get("context", {}),
            history_dir=history_dir,
            last_channel=data.get("last_channel"),
            last_user_id=data.get("last_user_id"),
            last_thread_ts=data.get("last_thread_ts"),
        )


@reflect_system("sessions")
class SessionManager:
    """Manages conversation sessions with file-based persistence.

    Sessions are indexed in memory for fast lookup and persisted to
    ``agents_dir/{agent_id}/sessions/{session_id}/session.json``.  A background
    reaper evicts idle sessions from the index (files are retained on disk).
    When ``daily_rollover=True``, sessions that have not been active today are
    automatically archived and a fresh session is created.

    Attributes:
        sessions (Dict[str, Session]): In-memory index of active sessions.
        user_sessions (Dict[str, List[str]]): Session IDs grouped by user_id.
        channel_sessions (Dict[str, List[str]]): Session IDs grouped by channel.
        max_sessions (int): Maximum number of sessions in the in-memory index.
        ttl_hours (int): Idle TTL in hours before a session is reaped from index.
        reaper_interval_minutes (int): How often the reaper loop runs.
        daily_rollover (bool): Whether to archive and restart sessions daily.
    """

    def __init__(
        self,
        max_sessions: int = 1000,
        session_timeout: int = 3600,  # kept for backwards-compat; unused
        persist_dir: Optional[str] = None,  # kept for backwards-compat; ignored
        agents_dir: Optional[str] = None,
        ttl_hours: int = 24,
        reaper_interval_minutes: int = 60,
        on_expire: Optional[Any] = None,
        daily_rollover: bool = True,
        on_rollover: Optional[Any] = None,  # async callable(session_id) — evict runner
    ):
        """Initialize the SessionManager.

        Args:
            max_sessions (int): Maximum sessions held in memory. Oldest inactive
                sessions are evicted when the limit is reached. Defaults to 1000.
            session_timeout (int): Kept for backwards-compatibility; not used.
                Defaults to 3600.
            persist_dir (Optional[str]): Kept for backwards-compatibility;
                ignored. Use agents_dir instead.
            agents_dir (Optional[str]): Root directory for per-agent session
                storage. Defaults to "~/.pyclawops/agents".
            ttl_hours (int): Hours of inactivity before a session is evicted
                from the in-memory index by the reaper. Defaults to 24.
            reaper_interval_minutes (int): How often the reaper loop runs.
                Defaults to 60.
            on_expire (Optional[Any]): Async callable invoked with the Session
                object just before it is evicted. Defaults to None.
            daily_rollover (bool): When True, sessions that were last active
                before today's local midnight are archived and replaced with a
                fresh session. Defaults to True.
            on_rollover (Optional[Any]): Async callable ``(session_id: str)``
                invoked after a session is archived during rollover so the agent
                can evict the old runner. Defaults to None.
        """
        self.sessions: Dict[str, Session] = {}
        self.user_sessions: Dict[str, List[str]] = {}
        self.channel_sessions: Dict[str, List[str]] = {}
        self.max_sessions = max_sessions
        self.ttl_hours = ttl_hours
        self.reaper_interval_minutes = reaper_interval_minutes
        self._on_expire = on_expire
        self.daily_rollover = daily_rollover
        self._on_rollover = on_rollover  # called with session_id after archiving
        # Resolve agents_dir: explicit arg > default
        _raw = agents_dir or _DEFAULT_AGENTS_DIR
        self._agents_dir: Path = Path(_raw).expanduser()
        self._reaper_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclawops.session")

    async def start(self) -> None:
        """Start the session manager.

        Loads existing sessions from disk into the in-memory index and
        starts the background reaper loop.
        """
        self._stop_event.clear()
        self._load_sessions_from_disk()
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        self._logger.info(
            f"Session manager started (ttl={self.ttl_hours}h, "
            f"reaper_interval={self.reaper_interval_minutes}m, "
            f"agents_dir={self._agents_dir})"
        )

    async def stop(self) -> None:
        """Stop the session manager by cancelling the reaper loop."""
        self._stop_event.set()
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
        self._logger.info("Session manager stopped")

    async def _reaper_loop(self) -> None:
        """Background loop that periodically evicts idle sessions from the index.

        Runs until the stop event is set.  Calls _reap_stale_sessions() at
        every reaper_interval_minutes interval.  Errors are logged and do not
        stop the loop.
        """
        interval = self.reaper_interval_minutes * 60
        while not self._stop_event.is_set():
            await asyncio.sleep(interval)
            if self._stop_event.is_set():
                break
            try:
                await self._reap_stale_sessions()
            except Exception as e:
                self._logger.error(f"Reaper error: {e}")

    async def _reap_stale_sessions(self) -> None:
        """Remove sessions that have been idle longer than ttl_hours from the
        in-memory index only — session files are kept on disk forever."""
        cutoff = now() - timedelta(hours=self.ttl_hours)
        to_reap = [
            s.id for s in self.sessions.values() if s.updated_at < cutoff
        ]
        for session_id in to_reap:
            session = self.sessions.get(session_id)
            if session and self._on_expire:
                try:
                    await self._on_expire(session)
                except Exception as exc:
                    self._logger.error(
                        f"on_expire callback failed for {session_id}: {exc}"
                    )
            self._remove_from_index(session_id)
        if to_reap:
            self._logger.info(
                f"Reaped {len(to_reap)} stale session(s) from index "
                f"(files retained on disk)"
            )

    # ------------------------------------------------------------------
    # Session directory helpers
    # ------------------------------------------------------------------

    def _session_dir(self, agent_id: str, session_id: str) -> Path:
        """Return the directory for a session.

        Args:
            agent_id (str): Agent identifier.
            session_id (str): Session identifier.

        Returns:
            Path: ``agents_dir/{agent_id}/sessions/{session_id}/``
        """
        return self._agents_dir / agent_id / "sessions" / session_id

    def _active_session_path(self, agent_id: str) -> Path:
        """Return the path to the active_session pointer file for an agent.

        Args:
            agent_id (str): Agent identifier.

        Returns:
            Path: ``agents_dir/{agent_id}/active_session``
        """
        return self._agents_dir / agent_id / "active_session"

    def set_active_session(self, agent_id: str, session_id: str) -> None:
        """Atomically write the active session pointer for an agent.

        Writes the session_id to the active_session file via a temp-file rename
        so the pointer is never half-written.

        Args:
            agent_id (str): Agent identifier.
            session_id (str): Session ID to record as the active session.
        """
        path = self._active_session_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(session_id)
        tmp.replace(path)

    async def get_active_session(self, agent_id: str) -> Optional["Session"]:
        """Return the agent's current active session, handling daily rollover.

        Returns None if no active session pointer exists.
        """
        path = self._active_session_path(agent_id)
        if not path.exists():
            return None
        try:
            session_id = path.read_text().strip()
        except Exception:
            return None
        if not session_id:
            return None

        # Fast path: already in memory
        session = self.sessions.get(session_id)
        if session is None:
            # Load from disk
            session_dir = self._session_dir(agent_id, session_id)
            meta_file = session_dir / "session.json"
            if not meta_file.exists():
                # Stale pointer — clear it
                try:
                    path.unlink()
                except Exception:
                    pass
                return None
            try:
                data = json.loads(meta_file.read_text())
                session = Session.from_dict(data, history_dir=session_dir)
                self.sessions[session.id] = session
                if session.user_id not in self.user_sessions:
                    self.user_sessions[session.user_id] = []
                if session.id not in self.user_sessions[session.user_id]:
                    self.user_sessions[session.user_id].append(session.id)
                if session.channel not in self.channel_sessions:
                    self.channel_sessions[session.channel] = []
                if session.id not in self.channel_sessions[session.channel]:
                    self.channel_sessions[session.channel].append(session.id)
            except Exception as e:
                self._logger.debug(f"Could not load active session {session_id}: {e}")
                try:
                    path.unlink()
                except Exception:
                    pass
                return None

        # Daily rollover
        if self.daily_rollover and self._is_before_today(session):
            new_session = await self._archive_and_rollover(session)
            self.set_active_session(agent_id, new_session.id)
            return new_session

        return session

    def _is_before_today(self, session: "Session") -> bool:
        """Return True if the session's last activity was before today's local midnight.

        Args:
            session (Session): The session to check.

        Returns:
            bool: True if ``session.updated_at`` is before today's midnight.
        """
        return session.updated_at < today_midnight()

    async def _archive_and_rollover(self, session: "Session") -> "Session":
        """Archive a stale session's history files and return a fresh replacement.

        Moves history.json and history_previous.json into an ``archived/``
        subdirectory with a timestamp suffix, calls the on_rollover callback to
        evict the old runner, removes the session from the index, and creates a
        new session preserving the routing context fields.

        Args:
            session (Session): The stale session to archive.

        Returns:
            Session: A newly created replacement session with the same routing
                context (last_channel, last_user_id, last_thread_ts).
        """
        import shutil as _shutil

        if session.history_dir and session.history_dir.exists():
            archive_dir = session.history_dir / "archived"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = now().strftime("%Y%m%d_%H%M%S")
            for hist_file in ["history.json", "history_previous.json"]:
                p = session.history_dir / hist_file
                if p.exists():
                    p.rename(archive_dir / f"{hist_file}.{stamp}")

        # Evict the old runner so the next turn starts clean
        if self._on_rollover:
            try:
                await self._on_rollover(session.id)
            except Exception as exc:
                self._logger.debug(f"Rollover evict callback failed for {session.id}: {exc}")

        # Remove old session from index (keeps disk files)
        self._remove_from_index(session.id)

        # Create and return a fresh session, preserving routing context
        new_session = await self.create_session(session.agent_id, session.channel, session.user_id)
        new_session.last_channel = session.last_channel
        new_session.last_user_id = session.last_user_id
        new_session.last_thread_ts = session.last_thread_ts
        new_session.save_metadata()
        self._logger.info(
            f"Daily rollover: archived {session.id} → new session {new_session.id} "
            f"for {session.user_id} on {session.channel}"
        )
        return new_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        ephemeral: bool = False,
    ) -> Session:
        """Create a new session.

        Parameters
        ----------
        ephemeral:
            When True the session is memory-only — no session.json is written to
            disk and ``history_dir`` is left as None.  Use this for isolated job
            sessions that must not accumulate on disk.
        """
        if len(self.sessions) >= self.max_sessions:
            await self._evict_oldest_session()

        session_id = _generate_session_id()
        history_dir = None if ephemeral else self._session_dir(agent_id, session_id)
        session = Session(
            id=session_id,
            agent_id=agent_id,
            channel=channel,
            user_id=user_id,
            metadata=metadata or {},
            history_dir=history_dir,
        )

        self.sessions[session.id] = session

        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = []
        self.user_sessions[user_id].append(session.id)

        if channel not in self.channel_sessions:
            self.channel_sessions[channel] = []
        self.channel_sessions[channel].append(session.id)

        if not ephemeral:
            session.save_metadata()
        self._logger.debug(
            f"Created session {session.id} for user {user_id} on {channel}"
            + (" (ephemeral)" if ephemeral else "")
        )
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Get a session from the in-memory index by ID, refreshing its timestamp.

        Args:
            session_id (str): Session identifier.

        Returns:
            Optional[Session]: The Session if found in the in-memory index
                (is_active is set to True and updated_at is refreshed), or
                None if not found.
        """
        session = self.sessions.get(session_id)
        if session:
            session.updated_at = now()
            session.is_active = True
        return session

    async def get_or_create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        create_if_not_exists: bool = True,
        ephemeral: bool = False,
    ) -> Optional[Session]:
        """Get existing session or create a new one.

        Search order:
        1. In-memory index (fast path).
        2. Disk scan — finds sessions evicted from memory by the reaper so
           that history is always resumed regardless of how long the gap was.
        3. Create a brand-new session (only when nothing exists on disk).
        """
        # 1. In-memory index
        if channel in self.channel_sessions:
            for session_id in reversed(self.channel_sessions[channel]):
                session = self.sessions.get(session_id)
                if (session and session.user_id == user_id
                        and session.agent_id == agent_id and session.is_active):
                    if self.daily_rollover and self._is_before_today(session):
                        return await self._archive_and_rollover(session)
                    session.updated_at = now()
                    return session

        # 2. Disk fallback — resume the most recent session even after reaper eviction
        session = self._find_most_recent_session_on_disk(agent_id, channel, user_id)
        if session is not None:
            if self.daily_rollover and self._is_before_today(session):
                # Stale session from a previous day — archive and start fresh
                return await self._archive_and_rollover(session)
            # Re-register in the index so future lookups hit the fast path
            session.is_active = True
            session.updated_at = now()
            self.sessions[session.id] = session
            if session.user_id not in self.user_sessions:
                self.user_sessions[session.user_id] = []
            if session.id not in self.user_sessions[session.user_id]:
                self.user_sessions[session.user_id].append(session.id)
            if channel not in self.channel_sessions:
                self.channel_sessions[channel] = []
            if session.id not in self.channel_sessions[channel]:
                self.channel_sessions[channel].append(session.id)
            self._logger.info(
                f"Resumed session {session.id} from disk for {user_id} on {channel}"
            )
            return session

        if create_if_not_exists:
            return await self.create_session(agent_id, channel, user_id, ephemeral=ephemeral)

        return None

    def _find_most_recent_session_on_disk(
        self, agent_id: str, channel: str, user_id: str
    ) -> Optional[Session]:
        """Scan disk for the most recent session matching agent/channel/user."""
        sessions_root = self._agents_dir / agent_id / "sessions"
        if not sessions_root.exists():
            return None
        best: Optional[Session] = None
        for sess_dir in sessions_root.iterdir():
            if not sess_dir.is_dir():
                continue
            # Skip sessions already in the index
            if sess_dir.name in self.sessions:
                continue
            meta_file = sess_dir / "session.json"
            if not meta_file.exists():
                continue
            try:
                data = json.loads(meta_file.read_text())
                if (data.get("channel") != channel
                        or str(data.get("user_id")) != str(user_id)
                        or data.get("agent_id") != agent_id):
                    continue
                session = Session.from_dict(data, history_dir=sess_dir)
                if best is None or session.updated_at > best.updated_at:
                    best = session
            except Exception as e:
                self._logger.debug(f"Could not read session {meta_file}: {e}")
        return best

    async def update_session(self, session_id: str, **updates) -> Optional[Session]:
        """Update arbitrary fields on a session in the in-memory index.

        Args:
            session_id (str): Session to update.
            **updates: Field name / value pairs to set on the session.

        Returns:
            Optional[Session]: The updated Session, or None if not found.
        """
        session = self.sessions.get(session_id)
        if not session:
            return None
        for key, value in updates.items():
            if hasattr(session, key):
                setattr(session, key, value)
        session.updated_at = now()
        return session

    async def delete_session(self, session_id: str) -> bool:
        """Remove a session from the in-memory index without deleting disk files.

        Args:
            session_id (str): Session to remove.

        Returns:
            bool: True if the session was found and removed; False otherwise.
        """
        return self._remove_from_index(session_id)

    async def list_sessions(
        self,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        user_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Session]:
        """List sessions from the in-memory index with optional filters.

        Results are sorted by updated_at descending (most recent first).

        Args:
            agent_id (Optional[str]): Filter by agent ID. Defaults to None (all).
            channel (Optional[str]): Filter by channel name. Defaults to None.
            user_id (Optional[str]): Filter by user ID. Defaults to None.
            active_only (bool): If True, only return sessions where is_active
                is True. Defaults to True.

        Returns:
            List[Session]: Matching sessions sorted by last activity.
        """
        sessions = list(self.sessions.values())
        if agent_id:
            sessions = [s for s in sessions if s.agent_id == agent_id]
        if channel:
            sessions = [s for s in sessions if s.channel == channel]
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]
        if active_only:
            sessions = [s for s in sessions if s.is_active]
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def list_sessions_sync(
        self,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        user_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Session]:
        """Synchronous version of list_sessions for use in TUI screens.

        Identical to list_sessions() but does not require an event loop.

        Args:
            agent_id (Optional[str]): Filter by agent ID. Defaults to None.
            channel (Optional[str]): Filter by channel name. Defaults to None.
            user_id (Optional[str]): Filter by user ID. Defaults to None.
            active_only (bool): Only return active sessions. Defaults to True.

        Returns:
            List[Session]: Matching sessions sorted by last activity descending.
        """
        sessions = list(self.sessions.values())
        if agent_id:
            sessions = [s for s in sessions if s.agent_id == agent_id]
        if channel:
            sessions = [s for s in sessions if s.channel == channel]
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]
        if active_only:
            sessions = [s for s in sessions if s.is_active]
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def get_status(self) -> Dict[str, Any]:
        """Return a status snapshot of the session manager.

        Returns:
            Dict[str, Any]: Dict with keys ``total_sessions``, ``active_sessions``,
                ``total_messages``, ``unique_users``, ``channels``.
        """
        return {
            "total_sessions": len(self.sessions),
            "active_sessions": len([s for s in self.sessions.values() if s.is_active]),
            "total_messages": sum(s.message_count for s in self.sessions.values()),
            "unique_users": len(self.user_sessions),
            "channels": list(self.channel_sessions.keys()),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remove_from_index(self, session_id: str) -> bool:
        """Remove a session from the in-memory index without touching disk files.

        Also cleans up the user_sessions and channel_sessions lookup dicts.

        Args:
            session_id (str): Session to remove.

        Returns:
            bool: True if the session was found and removed; False otherwise.
        """
        session = self.sessions.pop(session_id, None)
        if not session:
            return False

        if session.user_id in self.user_sessions:
            try:
                self.user_sessions[session.user_id].remove(session_id)
            except ValueError:
                pass
            if not self.user_sessions[session.user_id]:
                del self.user_sessions[session.user_id]

        if session.channel in self.channel_sessions:
            try:
                self.channel_sessions[session.channel].remove(session_id)
            except ValueError:
                pass
            if not self.channel_sessions[session.channel]:
                del self.channel_sessions[session.channel]

        self._logger.debug(f"Removed session {session_id} from index")
        return True

    # Kept for backward-compat (some callers use _remove_session directly)
    async def _remove_session(self, session_id: str) -> bool:
        """Backward-compatible async wrapper around _remove_from_index().

        Args:
            session_id (str): Session to remove.

        Returns:
            bool: True if removed; False if not found.
        """
        return self._remove_from_index(session_id)

    async def _evict_oldest_session(self) -> None:
        """Evict the oldest inactive session from the in-memory index.

        Called when the session count reaches max_sessions.  Only considers
        sessions where is_active is False; active sessions are never evicted
        by this method.
        """
        oldest: Optional[Session] = None
        for session in self.sessions.values():
            if not session.is_active:
                if oldest is None or session.updated_at < oldest.updated_at:
                    oldest = session
        if oldest:
            self._remove_from_index(oldest.id)
            self._logger.debug(f"Evicted oldest session {oldest.id}")

    def _load_sessions_from_disk(self) -> None:
        """Scan agents_dir for session.json files and load them into the index.

        Walks ``agents_dir/{agent_id}/sessions/{session_id}/session.json`` and
        reconstructs Session objects.  Sessions that fail to parse are logged
        as warnings and skipped.  Called once during start().
        """
        if not self._agents_dir.exists():
            return
        loaded = 0
        for agent_dir in self._agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            sessions_root = agent_dir / "sessions"
            if not sessions_root.exists():
                continue
            for sess_dir in sessions_root.iterdir():
                if not sess_dir.is_dir():
                    continue
                meta_file = sess_dir / "session.json"
                if not meta_file.exists():
                    continue
                try:
                    data = json.loads(meta_file.read_text())
                    session = Session.from_dict(data, history_dir=sess_dir)
                    self.sessions[session.id] = session
                    if session.user_id not in self.user_sessions:
                        self.user_sessions[session.user_id] = []
                    if session.id not in self.user_sessions[session.user_id]:
                        self.user_sessions[session.user_id].append(session.id)
                    if session.channel not in self.channel_sessions:
                        self.channel_sessions[session.channel] = []
                    if session.id not in self.channel_sessions[session.channel]:
                        self.channel_sessions[session.channel].append(session.id)
                    loaded += 1
                except Exception as e:
                    self._logger.warning(
                        f"Could not load session from {meta_file}: {e}"
                    )
        if loaded:
            self._logger.info(
                f"Loaded {loaded} session(s) from {self._agents_dir}"
            )
