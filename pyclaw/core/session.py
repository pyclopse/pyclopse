"""Session management for pyclaw."""

import asyncio
import json
import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclaw.core.router import IncomingMessage, OutgoingMessage

# Default agents directory
_DEFAULT_AGENTS_DIR = "~/.pyclaw/agents"

_SESSION_ALPHABET = string.ascii_letters + string.digits


def _generate_session_id() -> str:
    """Generate a date-prefixed session ID: YYYY-MM-DD-XXXXXX."""
    suffix = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(6))
    return f"{datetime.utcnow().strftime('%Y-%m-%d')}-{suffix}"


@dataclass
class Session:
    """A conversation session (metadata only — history lives on disk)."""

    id: str
    agent_id: str
    channel: str
    user_id: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    message_count: int = 0
    # Absolute path to the session directory (set by SessionManager)
    history_dir: Optional[Path] = field(default=None, repr=False, compare=False)

    @property
    def history_path(self) -> Optional[Path]:
        """Path to the primary history file."""
        if self.history_dir is None:
            return None
        return self.history_dir / "history.json"

    def touch(self, count_delta: int = 0) -> None:
        """Update last-activity timestamp and optionally increment message_count."""
        self.updated_at = datetime.utcnow()
        self.message_count += count_delta
        self.save_metadata()

    def save_metadata(self) -> None:
        """Persist session metadata to disk (atomic write)."""
        if self.history_dir is None:
            return
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            path = self.history_dir / "session.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2))
            tmp.replace(path)
        except Exception as e:
            logging.getLogger("pyclaw.session").error(
                f"Failed to save session metadata {self.id}: {e}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize session metadata (no message content)."""
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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], history_dir: Optional[Path] = None) -> "Session":
        """Restore a Session from a persisted metadata dict."""
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
        )


class SessionManager:
    """Manages multiple sessions with file-based persistence."""

    def __init__(
        self,
        max_sessions: int = 1000,
        session_timeout: int = 3600,
        persist_dir: Optional[str] = None,  # kept for backwards-compat; ignored
        agents_dir: Optional[str] = None,
        ttl_hours: int = 24,
        reaper_interval_minutes: int = 60,
        on_expire: Optional[Any] = None,
    ):
        self.sessions: Dict[str, Session] = {}
        self.user_sessions: Dict[str, List[str]] = {}
        self.channel_sessions: Dict[str, List[str]] = {}
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self.ttl_hours = ttl_hours
        self.reaper_interval_minutes = reaper_interval_minutes
        self._on_expire = on_expire
        # Resolve agents_dir: explicit arg > default
        _raw = agents_dir or _DEFAULT_AGENTS_DIR
        self._agents_dir: Path = Path(_raw).expanduser()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._reaper_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclaw.session")

    async def start(self) -> None:
        """Start the session manager."""
        self._stop_event.clear()
        self._load_sessions_from_disk()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        self._logger.info(
            f"Session manager started (ttl={self.ttl_hours}h, "
            f"reaper_interval={self.reaper_interval_minutes}m, "
            f"agents_dir={self._agents_dir})"
        )

    async def stop(self) -> None:
        """Stop the session manager."""
        self._stop_event.set()
        for task in (self._cleanup_task, self._reaper_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._logger.info("Session manager stopped")

    async def _cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cleanup_inactive()
            except Exception as e:
                self._logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)

    async def _reaper_loop(self) -> None:
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
        cutoff = datetime.utcnow() - timedelta(hours=self.ttl_hours)
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

    async def _cleanup_inactive(self) -> None:
        now = datetime.utcnow()
        to_remove = [
            s.id
            for s in self.sessions.values()
            if s.is_active
            and (now - s.updated_at).total_seconds() > self.session_timeout
        ]
        for session_id in to_remove:
            s = self.sessions.get(session_id)
            if s:
                s.is_active = False
            self._remove_from_index(session_id)

    # ------------------------------------------------------------------
    # Session directory helpers
    # ------------------------------------------------------------------

    def _session_dir(self, agent_id: str, session_id: str) -> Path:
        """Return the directory for a session: agents_dir/{agent_id}/sessions/{session_id}/"""
        return self._agents_dir / agent_id / "sessions" / session_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """Create a new session."""
        if len(self.sessions) >= self.max_sessions:
            await self._evict_oldest_session()

        session_id = _generate_session_id()
        history_dir = self._session_dir(agent_id, session_id)
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

        session.save_metadata()
        self._logger.debug(
            f"Created session {session.id} for user {user_id} on {channel}"
        )
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Get a session by ID."""
        session = self.sessions.get(session_id)
        if session:
            session.updated_at = datetime.utcnow()
            session.is_active = True
        return session

    async def get_or_create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        create_if_not_exists: bool = True,
    ) -> Optional[Session]:
        """Get existing active session or create a new one."""
        if channel in self.channel_sessions:
            for session_id in reversed(self.channel_sessions[channel]):
                session = self.sessions.get(session_id)
                if (session and session.user_id == user_id
                        and session.agent_id == agent_id and session.is_active):
                    session.updated_at = datetime.utcnow()
                    return session

        if create_if_not_exists:
            return await self.create_session(agent_id, channel, user_id)

        return None

    async def update_session(self, session_id: str, **updates) -> Optional[Session]:
        """Update session fields."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        for key, value in updates.items():
            if hasattr(session, key):
                setattr(session, key, value)
        session.updated_at = datetime.utcnow()
        return session

    async def delete_session(self, session_id: str) -> bool:
        """Remove a session from the in-memory index (files are not deleted)."""
        return self._remove_from_index(session_id)

    async def list_sessions(
        self,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        user_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Session]:
        """List sessions with optional filters."""
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
        """Synchronous version of list_sessions for TUI screens."""
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
        """Get session manager status."""
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
        """Remove a session from the in-memory index only."""
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
        return self._remove_from_index(session_id)

    async def _evict_oldest_session(self) -> None:
        """Evict the oldest inactive session from the index."""
        oldest: Optional[Session] = None
        for session in self.sessions.values():
            if not session.is_active:
                if oldest is None or session.updated_at < oldest.updated_at:
                    oldest = session
        if oldest:
            self._remove_from_index(oldest.id)
            self._logger.debug(f"Evicted oldest session {oldest.id}")

    def _load_sessions_from_disk(self) -> None:
        """Scan agents_dir for session.json files and load them into memory."""
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
