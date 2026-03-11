"""TODO data models."""

from __future__ import annotations

from datetime import datetime
from pyclaw.utils.time import now
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def score(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]

    @classmethod
    def parse(cls, value: str | int) -> "Priority":
        """Accept name ('high') or integer score (3)."""
        if isinstance(value, int):
            mapping = {1: cls.LOW, 2: cls.MEDIUM, 3: cls.HIGH, 4: cls.CRITICAL}
            if value not in mapping:
                raise ValueError(f"Priority score must be 1-4, got {value}")
            return mapping[value]
        return cls(str(value).lower())


class TodoStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


def _short_id() -> str:
    return uuid4().hex[:8]


class Todo(BaseModel):
    """A single TODO item."""

    id: str = Field(default_factory=_short_id)
    title: str
    description: str = ""
    priority: Priority = Priority.MEDIUM
    status: TodoStatus = TodoStatus.OPEN
    owner: Optional[str] = None          # X-Agent-Name or None (human-created)
    tags: list[str] = Field(default_factory=list)
    due_date: Optional[datetime] = None
    blocked_by: Optional[str] = None     # ID of another Todo this depends on
    notes: str = ""                       # progress / completion notes
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    def touch(self) -> None:
        self.updated_at = now()

    def summary(self) -> str:
        """One-line summary for list views."""
        due = f" due={self.due_date.date()}" if self.due_date else ""
        tags = f" [{','.join(self.tags)}]" if self.tags else ""
        owner = f" @{self.owner}" if self.owner else ""
        return (
            f"[{self.id}] [{self.priority.value.upper()}] [{self.status.value}]"
            f" {self.title}{due}{tags}{owner}"
        )
