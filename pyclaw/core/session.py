"""Session management for pyclaw."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclaw.core.router import IncomingMessage, OutgoingMessage


@dataclass
class Message:
    """A message in a session."""
    id: str
    role: str  # system, user, assistant
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_results: Optional[List[Dict[str, Any]]] = None


@dataclass
class Session:
    """A conversation session."""
    id: str
    agent_id: str
    channel: str
    user_id: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    message_count: int = 0
    # Injected by SessionManager so add_message auto-persists
    _persist_fn: Any = field(default=None, repr=False, compare=False)

    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """Add a message to the session."""
        message = Message(
            id=str(uuid.uuid4()),
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.messages.append(message)
        self.updated_at = datetime.utcnow()
        self.message_count += 1
        if self._persist_fn is not None:
            self._persist_fn(self)
        return message
    
    def get_messages_for_provider(self) -> List[Dict[str, Any]]:
        """Get messages in format for provider API."""
        return [
            {
                "role": msg.role,
                "content": msg.content,
            }
            for msg in self.messages
        ]
    
    def get_context_window(self, max_messages: int = 20) -> List[Message]:
        """Get recent messages within context window."""
        return self.messages[-max_messages:]
    
    def clear_messages(self) -> None:
        """Clear all messages but keep session."""
        self.messages.clear()
        self.updated_at = datetime.utcnow()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (summary, no messages)."""
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
                if not k.startswith("_")  # skip non-serialisable runner refs
            },
        }

    def to_full_dict(self) -> Dict[str, Any]:
        """Serialize full session including message history."""
        d = self.to_dict()
        d["messages"] = [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
                "metadata": m.metadata,
            }
            for m in self.messages
        ]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        """Restore a Session from a persisted dict."""
        msgs = []
        for md in data.get("messages", []):
            msgs.append(
                Message(
                    id=md["id"],
                    role=md["role"],
                    content=md["content"],
                    timestamp=datetime.fromisoformat(md["timestamp"]),
                    metadata=md.get("metadata", {}),
                )
            )
        return cls(
            id=data["id"],
            agent_id=data["agent_id"],
            channel=data["channel"],
            user_id=data["user_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            messages=msgs,
            message_count=data.get("message_count", len(msgs)),
            is_active=data.get("is_active", True),
            metadata=data.get("metadata", {}),
            context=data.get("context", {}),
        )


class SessionManager:
    """Manages multiple sessions."""

    def __init__(
        self,
        max_sessions: int = 1000,
        session_timeout: int = 3600,
        persist_dir: Optional[str] = None,
        ttl_hours: int = 24,
        reaper_interval_minutes: int = 60,
        on_expire: Optional[Any] = None,
    ):
        self.sessions: Dict[str, Session] = {}
        self.user_sessions: Dict[str, List[str]] = {}  # user_id -> session_ids
        self.channel_sessions: Dict[str, List[str]] = {}  # channel -> session_ids
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self.ttl_hours = ttl_hours
        self.reaper_interval_minutes = reaper_interval_minutes
        # Optional async callable fired when the reaper evicts a session:
        #   async def on_expire(session: Session) -> None
        self._on_expire = on_expire
        self._persist_dir: Optional[Path] = (
            Path(persist_dir).expanduser() if persist_dir else None
        )
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
            f"reaper_interval={self.reaper_interval_minutes}m)"
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
        """Periodic cleanup of inactive sessions."""
        while not self._stop_event.is_set():
            try:
                await self._cleanup_inactive()
            except Exception as e:
                self._logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)  # Run every minute

    async def _reaper_loop(self) -> None:
        """Reap sessions that have been idle longer than ttl_hours."""
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
        """Remove sessions that have been idle longer than ttl_hours."""
        cutoff = datetime.utcnow() - timedelta(hours=self.ttl_hours)
        to_reap = [
            s.id
            for s in self.sessions.values()
            if s.updated_at < cutoff
        ]
        for session_id in to_reap:
            session = self.sessions.get(session_id)
            if session and self._on_expire:
                try:
                    await self._on_expire(session)
                except Exception as exc:
                    self._logger.error(f"on_expire callback failed for {session_id}: {exc}")
            await self._remove_session(session_id)
        if to_reap:
            self._logger.info(f"Reaped {len(to_reap)} stale session(s)")
    
    async def _cleanup_inactive(self) -> None:
        """Remove inactive sessions that have timed out."""
        now = datetime.utcnow()
        to_remove = []
        
        for session in self.sessions.values():
            if not session.is_active:
                continue
            
            # Check if session has timed out
            age = (now - session.updated_at).total_seconds()
            if age > self.session_timeout:
                session.is_active = False
                to_remove.append(session.id)
        
        for session_id in to_remove:
            await self._remove_session(session_id)
    
    async def create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """Create a new session."""
        # Check max sessions
        if len(self.sessions) >= self.max_sessions:
            await self._evict_oldest_session()
        
        session = Session(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            channel=channel,
            user_id=user_id,
            metadata=metadata or {},
            _persist_fn=self._write_session if self._persist_dir else None,
        )

        self.sessions[session.id] = session
        
        # Track by user
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = []
        self.user_sessions[user_id].append(session.id)
        
        # Track by channel
        if channel not in self.channel_sessions:
            self.channel_sessions[channel] = []
        self.channel_sessions[channel].append(session.id)
        
        self._write_session(session)
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
        """Get existing session or create new one."""
        # Try to find existing active session for this user/channel
        if channel in self.channel_sessions:
            for session_id in reversed(self.channel_sessions[channel]):
                session = self.sessions.get(session_id)
                if session and session.user_id == user_id and session.is_active:
                    session.updated_at = datetime.utcnow()
                    return session
        
        # Create new session
        if create_if_not_exists:
            return await self.create_session(agent_id, channel, user_id)
        
        return None
    
    async def update_session(
        self,
        session_id: str,
        **updates,
    ) -> Optional[Session]:
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
        """Delete a session."""
        return await self._remove_session(session_id)
    
    async def _remove_session(self, session_id: str) -> bool:
        """Internal method to remove a session."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return False
        
        # Remove from user_sessions
        if session.user_id in self.user_sessions:
            self.user_sessions[session.user_id].remove(session_id)
            if not self.user_sessions[session.user_id]:
                del self.user_sessions[session.user_id]
        
        # Remove from channel_sessions
        if session.channel in self.channel_sessions:
            self.channel_sessions[session.channel].remove(session_id)
            if not self.channel_sessions[session.channel]:
                del self.channel_sessions[session.channel]
        
        self._delete_session_file(session_id)
        self._logger.debug(f"Removed session {session_id}")
        return True
    
    async def _evict_oldest_session(self) -> None:
        """Evict the oldest inactive session."""
        oldest: Optional[Session] = None

        for session in self.sessions.values():
            if not session.is_active:
                if oldest is None or session.updated_at < oldest.updated_at:
                    oldest = session

        if oldest:
            await self._remove_session(oldest.id)
            self._logger.debug(f"Evicted oldest session {oldest.id}")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _session_path(self, session_id: str) -> Optional[Path]:
        if self._persist_dir is None:
            return None
        return self._persist_dir / f"{session_id}.json"

    def _write_session(self, session: Session) -> None:
        """Synchronously write one session to disk (atomic)."""
        path = self._session_path(session.id)
        if path is None:
            return
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(session.to_full_dict(), indent=2))
            tmp.replace(path)
        except Exception as e:
            self._logger.error(f"Failed to persist session {session.id}: {e}")

    def _delete_session_file(self, session_id: str) -> None:
        path = self._session_path(session_id)
        if path and path.exists():
            try:
                path.unlink()
            except Exception as e:
                self._logger.error(f"Failed to delete session file {session_id}: {e}")

    def _load_sessions_from_disk(self) -> None:
        """Load all persisted sessions into memory on startup."""
        if self._persist_dir is None or not self._persist_dir.exists():
            return
        loaded = 0
        for p in self._persist_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                session = Session.from_dict(data)
                session._persist_fn = self._write_session if self._persist_dir else None
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
                self._logger.warning(f"Could not load session from {p.name}: {e}")
        if loaded:
            self._logger.info(f"Loaded {loaded} sessions from {self._persist_dir}")
    
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
        
        # Sort by updated_at descending
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        
        return sessions
    
    def list_sessions_sync(
        self,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        user_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Session]:
        """Synchronous version of list_sessions for use from screens."""
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
