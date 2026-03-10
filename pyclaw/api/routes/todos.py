"""TODO registry API routes."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pyclaw.todos.models import Priority, Todo, TodoStatus

logger = logging.getLogger("pyclaw.api.todos")
router = APIRouter()


def _store():
    from pyclaw.api.app import get_gateway
    gw = get_gateway()
    store = getattr(gw, "_todo_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="TODO store not initialised")
    return store


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateTodoRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"   # low|medium|high|critical or 1-4
    tags: List[str] = []
    due_date: Optional[str] = None   # ISO date/datetime string
    blocked_by: Optional[str] = None
    owner: Optional[str] = None


class UpdateTodoRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[List[str]] = None
    due_date: Optional[str] = None
    blocked_by: Optional[str] = None
    notes: Optional[str] = None


class MarkTodoRequest(BaseModel):
    status: str   # open|in_progress|done|cancelled|blocked
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_priority(value: str) -> Priority:
    try:
        return Priority.parse(int(value) if value.isdigit() else value)
    except (ValueError, KeyError):
        raise HTTPException(status_code=422, detail=f"Invalid priority: {value!r}")


def _parse_status(value: str) -> TodoStatus:
    try:
        return TodoStatus(value.lower())
    except ValueError:
        valid = [s.value for s in TodoStatus]
        raise HTTPException(status_code=422, detail=f"Invalid status {value!r}. Valid: {valid}")


def _parse_due(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {value!r}")


def _todo_dict(t: Todo) -> Dict[str, Any]:
    return t.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=Dict[str, Any])
async def list_todos(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    tags: Optional[str] = None,   # comma-separated
    owner: Optional[str] = None,
    all_owners: bool = False,
) -> Dict[str, Any]:
    store = _store()
    s = _parse_status(status) if status else None
    p = _parse_priority(priority) if priority else None
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    items = await store.list(status=s, priority=p, tags=tag_list, owner=owner, all_owners=all_owners)
    return {"todos": [_todo_dict(t) for t in items], "total": len(items)}


@router.post("/", response_model=Dict[str, Any], status_code=201)
async def create_todo(req: CreateTodoRequest) -> Dict[str, Any]:
    store = _store()
    todo = Todo(
        title=req.title,
        description=req.description,
        priority=_parse_priority(req.priority),
        tags=req.tags,
        due_date=_parse_due(req.due_date),
        blocked_by=req.blocked_by,
        owner=req.owner,
    )
    await store.create(todo)
    return {"todo": _todo_dict(todo)}


@router.get("/next", response_model=Dict[str, Any])
async def next_todo(owner: Optional[str] = None, all_owners: bool = False) -> Dict[str, Any]:
    store = _store()
    todo = await store.next_todo(owner=owner, all_owners=all_owners)
    if not todo:
        return {"todo": None, "message": "No open unblocked todos found."}
    return {"todo": _todo_dict(todo)}


@router.get("/{todo_id}", response_model=Dict[str, Any])
async def get_todo(todo_id: str) -> Dict[str, Any]:
    store = _store()
    todo = await store.get(todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"todo": _todo_dict(todo)}


@router.patch("/{todo_id}", response_model=Dict[str, Any])
async def update_todo(todo_id: str, req: UpdateTodoRequest) -> Dict[str, Any]:
    store = _store()
    updates: Dict[str, Any] = {}
    if req.title is not None:
        updates["title"] = req.title
    if req.description is not None:
        updates["description"] = req.description
    if req.priority is not None:
        updates["priority"] = _parse_priority(req.priority)
    if req.tags is not None:
        updates["tags"] = req.tags
    if req.due_date is not None:
        updates["due_date"] = _parse_due(req.due_date)
    if req.blocked_by is not None:
        updates["blocked_by"] = req.blocked_by
    if req.notes is not None:
        updates["notes"] = req.notes
    todo = await store.update(todo_id, **updates)
    if not todo:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"todo": _todo_dict(todo)}


@router.post("/{todo_id}/mark", response_model=Dict[str, Any])
async def mark_todo(todo_id: str, req: MarkTodoRequest) -> Dict[str, Any]:
    store = _store()
    status = _parse_status(req.status)
    todo = await store.mark(todo_id, status, notes=req.notes)
    if not todo:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"todo": _todo_dict(todo)}


@router.delete("/{todo_id}", response_model=Dict[str, Any])
async def delete_todo(todo_id: str) -> Dict[str, Any]:
    store = _store()
    deleted = await store.delete(todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"deleted": todo_id}
