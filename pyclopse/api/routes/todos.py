"""TODO registry API routes."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pyclopse.todos.models import Priority, Todo, TodoStatus

logger = logging.getLogger("pyclopse.api.todos")
router = APIRouter()


def _store():
    """Retrieve the gateway's TODO store.

    Returns:
        TodoStore: The active TODO store instance.

    Raises:
        HTTPException: With status 503 if the store has not been initialised.
    """
    from pyclopse.api.app import get_gateway
    gw = get_gateway()
    store = getattr(gw, "_todo_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="TODO store not initialised")
    return store


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateTodoRequest(BaseModel):
    """Request body for creating a new TODO item.

    Attributes:
        title (str): Short title for the TODO.
        description (str): Optional longer description. Defaults to "".
        priority (str): Priority level — "low", "medium", "high", "critical",
            or an integer score 1–4. Defaults to "medium".
        tags (List[str]): Classification tags. Defaults to [].
        due_date (Optional[str]): ISO date or datetime string for the deadline.
        blocked_by (Optional[str]): ID of another TODO this depends on.
        owner (Optional[str]): Agent name or identifier that owns this TODO.
    """

    title: str
    description: str = ""
    priority: str = "medium"   # low|medium|high|critical or 1-4
    tags: List[str] = []
    due_date: Optional[str] = None   # ISO date/datetime string
    blocked_by: Optional[str] = None
    owner: Optional[str] = None


class UpdateTodoRequest(BaseModel):
    """Partial update payload for an existing TODO.

    All fields are optional; only fields explicitly provided are applied.

    Attributes:
        title (Optional[str]): New title.
        description (Optional[str]): New description.
        priority (Optional[str]): New priority level.
        tags (Optional[List[str]]): Replacement tag list.
        due_date (Optional[str]): New ISO due date/datetime string.
        blocked_by (Optional[str]): New blocker TODO ID.
        notes (Optional[str]): Progress or completion notes.
    """

    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[List[str]] = None
    due_date: Optional[str] = None
    blocked_by: Optional[str] = None
    notes: Optional[str] = None


class MarkTodoRequest(BaseModel):
    """Request body for transitioning a TODO to a new status.

    Attributes:
        status (str): Target status — one of "open", "in_progress", "done",
            "cancelled", or "blocked".
        notes (Optional[str]): Optional progress or completion notes to record.
    """

    status: str   # open|in_progress|done|cancelled|blocked
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_priority(value: str) -> Priority:
    """Parse a priority string or integer score into a Priority enum member.

    Args:
        value (str): Priority name ("low", "medium", "high", "critical") or
            integer score as string ("1"–"4").

    Returns:
        Priority: The corresponding Priority enum member.

    Raises:
        HTTPException: With status 422 if the value cannot be parsed.
    """
    try:
        return Priority.parse(int(value) if value.isdigit() else value)
    except (ValueError, KeyError):
        raise HTTPException(status_code=422, detail=f"Invalid priority: {value!r}")


def _parse_status(value: str) -> TodoStatus:
    """Parse a status string into a TodoStatus enum member.

    Args:
        value (str): Status name — one of "open", "in_progress", "done",
            "cancelled", or "blocked".

    Returns:
        TodoStatus: The corresponding TodoStatus enum member.

    Raises:
        HTTPException: With status 422 listing valid values if the string is
            not recognised.
    """
    try:
        return TodoStatus(value.lower())
    except ValueError:
        valid = [s.value for s in TodoStatus]
        raise HTTPException(status_code=422, detail=f"Invalid status {value!r}. Valid: {valid}")


def _parse_due(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO date/datetime string into a naive datetime.

    Args:
        value (Optional[str]): ISO 8601 date or datetime string. "Z" suffix
            is accepted and treated as UTC. None returns None.

    Returns:
        Optional[datetime]: Timezone-naive datetime, or None if value is empty.

    Raises:
        HTTPException: With status 422 if the string cannot be parsed.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {value!r}")


def _todo_dict(t: Todo) -> Dict[str, Any]:
    """Serialise a Todo to a JSON-friendly dictionary.

    Args:
        t (Todo): The Todo instance to serialise.

    Returns:
        Dict[str, Any]: Todo fields serialised via ``model_dump(mode="json")``.
    """
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
    """List TODO items with optional filters.

    Args:
        status (Optional[str]): Filter by status ("open", "done", etc.).
        priority (Optional[str]): Filter by priority name or score.
        tags (Optional[str]): Comma-separated tag list to filter by.
        owner (Optional[str]): Agent name to filter by owner.  When supplied
            and ``all_owners`` is False, also includes human-created items
            (owner=None).
        all_owners (bool): Return items for all owners when True.

    Returns:
        Dict[str, Any]: ``{"todos": [...], "total": int}`` sorted by
            priority descending then created_at ascending.
    """
    store = _store()
    s = _parse_status(status) if status else None
    p = _parse_priority(priority) if priority else None
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    items = await store.list(status=s, priority=p, tags=tag_list, owner=owner, all_owners=all_owners)
    return {"todos": [_todo_dict(t) for t in items], "total": len(items)}


@router.post("/", response_model=Dict[str, Any], status_code=201)
async def create_todo(req: CreateTodoRequest) -> Dict[str, Any]:
    """Create a new TODO item.

    Args:
        req (CreateTodoRequest): TODO creation payload.

    Returns:
        Dict[str, Any]: ``{"todo": {...}}`` with the newly created item.

    Raises:
        HTTPException: With status 422 if priority or due_date are invalid.
    """
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
    """Get the highest-priority open unblocked TODO item.

    Args:
        owner (Optional[str]): Agent name to filter by owner. When supplied and
            ``all_owners`` is False, also includes human-created items (owner=None).
        all_owners (bool): Return items for all owners when True.

    Returns:
        Dict[str, Any]: ``{"todo": {...}}`` with the next actionable TODO, or
            ``{"todo": None, "message": "No open unblocked todos found."}`` when
            the queue is empty.
    """
    store = _store()
    todo = await store.next_todo(owner=owner, all_owners=all_owners)
    if not todo:
        return {"todo": None, "message": "No open unblocked todos found."}
    return {"todo": _todo_dict(todo)}


@router.get("/{todo_id}", response_model=Dict[str, Any])
async def get_todo(todo_id: str) -> Dict[str, Any]:
    """Get a single TODO item by ID.

    Args:
        todo_id (str): Unique identifier of the TODO to retrieve.

    Returns:
        Dict[str, Any]: ``{"todo": {...}}`` with the requested TODO item.

    Raises:
        HTTPException: With status 404 if no TODO with the given ID exists.
    """
    store = _store()
    todo = await store.get(todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"todo": _todo_dict(todo)}


@router.patch("/{todo_id}", response_model=Dict[str, Any])
async def update_todo(todo_id: str, req: UpdateTodoRequest) -> Dict[str, Any]:
    """Partially update an existing TODO item.

    Only fields explicitly provided in the request body are applied; omitted
    fields retain their current values.

    Args:
        todo_id (str): Unique identifier of the TODO to update.
        req (UpdateTodoRequest): Partial update payload with fields to change.

    Returns:
        Dict[str, Any]: ``{"todo": {...}}`` with the updated TODO item.

    Raises:
        HTTPException: With status 404 if no TODO with the given ID exists.
        HTTPException: With status 422 if priority or due_date are invalid.
    """
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
    """Transition a TODO item to a new status.

    Args:
        todo_id (str): Unique identifier of the TODO to update.
        req (MarkTodoRequest): Status transition payload with target status and
            optional notes.

    Returns:
        Dict[str, Any]: ``{"todo": {...}}`` with the updated TODO item.

    Raises:
        HTTPException: With status 404 if no TODO with the given ID exists.
        HTTPException: With status 422 if the status value is not recognised.
    """
    store = _store()
    status = _parse_status(req.status)
    todo = await store.mark(todo_id, status, notes=req.notes)
    if not todo:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"todo": _todo_dict(todo)}


@router.delete("/{todo_id}", response_model=Dict[str, Any])
async def delete_todo(todo_id: str) -> Dict[str, Any]:
    """Permanently delete a TODO item.

    Args:
        todo_id (str): Unique identifier of the TODO to delete.

    Returns:
        Dict[str, Any]: ``{"deleted": todo_id}`` confirming the deletion.

    Raises:
        HTTPException: With status 404 if no TODO with the given ID exists.
    """
    store = _store()
    deleted = await store.delete(todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Todo not found: {todo_id!r}")
    return {"deleted": todo_id}
