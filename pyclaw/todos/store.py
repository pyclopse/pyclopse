"""Async-safe JSON persistence for the TODO registry."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pyclaw.utils.time import now
from pathlib import Path
from typing import Optional

from .models import Priority, Todo, TodoStatus

logger = logging.getLogger(__name__)


class TodoStore:
    """Load/save Todos as JSON; all mutating methods acquire an asyncio lock."""

    def __init__(self, persist_path: str = "~/.pyclaw/todos.json") -> None:
        self._path = Path(persist_path).expanduser()
        self._todos: dict[str, Todo] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_sync(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            self._todos = {k: Todo.model_validate(v) for k, v in raw.items()}
            logger.debug(f"Loaded {len(self._todos)} todos from {self._path}")
        except Exception as e:
            logger.error(f"Failed to load todos: {e}")

    def _save_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in self._todos.items()}
        self._path.write_text(json.dumps(data, indent=2, default=str))

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_sync()
            self._loaded = True

    async def _save(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._save_sync)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, todo: Todo) -> Todo:
        async with self._lock:
            await self._ensure_loaded()
            self._todos[todo.id] = todo
            await self._save()
        return todo

    async def get(self, todo_id: str) -> Optional[Todo]:
        await self._ensure_loaded()
        return self._todos.get(todo_id)

    async def list(
        self,
        status: Optional[TodoStatus] = None,
        priority: Optional[Priority] = None,
        tags: Optional[list[str]] = None,
        owner: Optional[str] = None,
        all_owners: bool = False,
    ) -> list[Todo]:
        await self._ensure_loaded()
        items = list(self._todos.values())

        if not all_owners and owner is not None:
            # Show caller's todos + human-created (owner=None) ones
            items = [t for t in items if t.owner == owner or t.owner is None]

        if status is not None:
            items = [t for t in items if t.status == status]
        if priority is not None:
            items = [t for t in items if t.priority == priority]
        if tags:
            tag_set = {t.lower() for t in tags}
            items = [t for t in items if tag_set.intersection(x.lower() for x in t.tags)]

        # Sort: priority desc (critical first), then created_at asc (oldest first)
        items.sort(key=lambda t: (-t.priority.score, t.created_at))
        return items

    async def update(self, todo_id: str, **fields) -> Optional[Todo]:
        async with self._lock:
            await self._ensure_loaded()
            todo = self._todos.get(todo_id)
            if todo is None:
                return None
            for k, v in fields.items():
                if v is not None and hasattr(todo, k):
                    setattr(todo, k, v)
            todo.touch()
            await self._save()
        return todo

    async def mark(
        self,
        todo_id: str,
        status: TodoStatus,
        notes: Optional[str] = None,
    ) -> Optional[Todo]:
        async with self._lock:
            await self._ensure_loaded()
            todo = self._todos.get(todo_id)
            if todo is None:
                return None
            todo.status = status
            if notes is not None:
                todo.notes = notes
            if status == TodoStatus.DONE:
                todo.completed_at = now()
            todo.touch()
            await self._save()
        return todo

    async def delete(self, todo_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            if todo_id not in self._todos:
                return False
            del self._todos[todo_id]
            await self._save()
        return True

    async def next_todo(
        self,
        owner: Optional[str] = None,
        all_owners: bool = False,
    ) -> Optional[Todo]:
        """Return the oldest highest-priority open unblocked TODO."""
        items = await self.list(
            status=TodoStatus.OPEN,
            owner=owner,
            all_owners=all_owners,
        )
        # Filter out blocked todos (blocked_by points to an open/in-progress todo)
        unblocked = []
        todo_ids = {t.id for t in self._todos.values()}
        open_ids = {
            t.id for t in self._todos.values()
            if t.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS)
        }
        for t in items:
            if t.blocked_by and t.blocked_by in open_ids:
                continue  # dependency not yet resolved
            unblocked.append(t)

        if not unblocked:
            return None
        # Already sorted: priority desc, created_at asc → first item is what we want
        return unblocked[0]
