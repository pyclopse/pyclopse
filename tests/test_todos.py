"""
Tests for the pyclopse TODO registry.

Covers:
  - Priority / TodoStatus models
  - TodoStore CRUD + filtering + sorting
  - todos_next (oldest highest-priority unblocked)
  - REST API routes (via TestClient)
  - MCP tools: todos_list, todo_get, todo_create, todo_update, todo_mark,
               todo_delete, todos_next
"""
import json
import os
import tempfile
from datetime import datetime, timedelta
from pyclopse.utils.time import now
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pyclopse.todos.models import Priority, Todo, TodoStatus
from pyclopse.todos.store import TodoStore


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestPriority:

    def test_parse_name(self):
        assert Priority.parse("high") == Priority.HIGH
        assert Priority.parse("CRITICAL") == Priority.CRITICAL

    def test_parse_int(self):
        assert Priority.parse(1) == Priority.LOW
        assert Priority.parse(4) == Priority.CRITICAL

    def test_score_ordering(self):
        assert Priority.LOW.score < Priority.MEDIUM.score
        assert Priority.MEDIUM.score < Priority.HIGH.score
        assert Priority.HIGH.score < Priority.CRITICAL.score

    def test_invalid_int_raises(self):
        with pytest.raises(ValueError):
            Priority.parse(5)

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError):
            Priority.parse("urgent")


class TestTodoModel:

    def test_default_fields(self):
        t = Todo(title="Test task")
        assert t.status == TodoStatus.OPEN
        assert t.priority == Priority.MEDIUM
        assert t.owner is None
        assert len(t.id) == 8

    def test_summary_contains_key_info(self):
        t = Todo(title="My task", priority=Priority.HIGH, status=TodoStatus.IN_PROGRESS)
        s = t.summary()
        assert "My task" in s
        assert "HIGH" in s
        assert "in_progress" in s


# ---------------------------------------------------------------------------
# TodoStore unit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return TodoStore(persist_path=str(tmp_path / "todos.json"))


@pytest.fixture
def sample_todos(store):
    """Pre-populate the store with a variety of todos."""
    return store  # tests create items themselves


class TestTodoStore:

    @pytest.mark.asyncio
    async def test_create_and_get(self, store):
        t = Todo(title="First task", priority=Priority.HIGH)
        await store.create(t)
        got = await store.get(t.id)
        assert got is not None
        assert got.title == "First task"

    @pytest.mark.asyncio
    async def test_get_missing(self, store):
        assert await store.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_all(self, store):
        await store.create(Todo(title="A"))
        await store.create(Todo(title="B"))
        items = await store.list(all_owners=True)
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_list_filter_status(self, store):
        await store.create(Todo(title="open1"))
        t2 = Todo(title="done1")
        t2.status = TodoStatus.DONE
        await store.create(t2)
        open_items = await store.list(status=TodoStatus.OPEN, all_owners=True)
        assert len(open_items) == 1
        assert open_items[0].title == "open1"

    @pytest.mark.asyncio
    async def test_list_filter_priority(self, store):
        await store.create(Todo(title="low", priority=Priority.LOW))
        await store.create(Todo(title="critical", priority=Priority.CRITICAL))
        highs = await store.list(priority=Priority.CRITICAL, all_owners=True)
        assert len(highs) == 1
        assert highs[0].title == "critical"

    @pytest.mark.asyncio
    async def test_list_filter_tags(self, store):
        await store.create(Todo(title="tagged", tags=["infra", "urgent"]))
        await store.create(Todo(title="untagged"))
        tagged = await store.list(tags=["infra"], all_owners=True)
        assert len(tagged) == 1
        assert tagged[0].title == "tagged"

    @pytest.mark.asyncio
    async def test_list_owner_filter(self, store):
        await store.create(Todo(title="mine", owner="agent-a"))
        await store.create(Todo(title="theirs", owner="agent-b"))
        await store.create(Todo(title="human"))  # owner=None

        # agent-a sees own + human-created
        mine = await store.list(owner="agent-a")
        titles = {t.title for t in mine}
        assert "mine" in titles
        assert "human" in titles
        assert "theirs" not in titles

    @pytest.mark.asyncio
    async def test_list_sort_priority_then_age(self, store):
        """Critical items come before high; within same priority, oldest first."""
        old_high = Todo(title="old-high", priority=Priority.HIGH)
        old_high.created_at = now() - timedelta(hours=2)
        new_high = Todo(title="new-high", priority=Priority.HIGH)
        critical = Todo(title="critical", priority=Priority.CRITICAL)
        for t in [new_high, old_high, critical]:
            await store.create(t)

        items = await store.list(all_owners=True)
        assert items[0].title == "critical"
        assert items[1].title == "old-high"
        assert items[2].title == "new-high"

    @pytest.mark.asyncio
    async def test_update(self, store):
        t = await store.create(Todo(title="original"))
        updated = await store.update(t.id, title="updated", priority=Priority.HIGH)
        assert updated.title == "updated"
        assert updated.priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_update_missing(self, store):
        assert await store.update("nope", title="x") is None

    @pytest.mark.asyncio
    async def test_mark_done(self, store):
        t = await store.create(Todo(title="task"))
        marked = await store.mark(t.id, TodoStatus.DONE, notes="finished it")
        assert marked.status == TodoStatus.DONE
        assert marked.notes == "finished it"
        assert marked.completed_at is not None

    @pytest.mark.asyncio
    async def test_mark_in_progress(self, store):
        t = await store.create(Todo(title="task"))
        marked = await store.mark(t.id, TodoStatus.IN_PROGRESS)
        assert marked.status == TodoStatus.IN_PROGRESS
        assert marked.completed_at is None

    @pytest.mark.asyncio
    async def test_mark_missing(self, store):
        assert await store.mark("nope", TodoStatus.DONE) is None

    @pytest.mark.asyncio
    async def test_delete(self, store):
        t = await store.create(Todo(title="to delete"))
        assert await store.delete(t.id) is True
        assert await store.get(t.id) is None

    @pytest.mark.asyncio
    async def test_delete_missing(self, store):
        assert await store.delete("nope") is False

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        """Data survives a store reload."""
        path = str(tmp_path / "todos.json")
        s1 = TodoStore(persist_path=path)
        t = await s1.create(Todo(title="persisted", priority=Priority.HIGH))

        s2 = TodoStore(persist_path=path)
        got = await s2.get(t.id)
        assert got is not None
        assert got.title == "persisted"
        assert got.priority == Priority.HIGH


class TestTodosNext:

    @pytest.mark.asyncio
    async def test_returns_oldest_highest_priority(self, store):
        old_high = Todo(title="old-high", priority=Priority.HIGH)
        old_high.created_at = now() - timedelta(hours=3)
        new_high = Todo(title="new-high", priority=Priority.HIGH)
        critical = Todo(title="critical", priority=Priority.CRITICAL)
        for t in [new_high, old_high, critical]:
            await store.create(t)
        nxt = await store.next_todo(all_owners=True)
        assert nxt.title == "critical"

    @pytest.mark.asyncio
    async def test_skips_blocked(self, store):
        blocker = await store.create(Todo(title="blocker", priority=Priority.LOW))
        blocked = Todo(title="blocked", priority=Priority.CRITICAL, blocked_by=blocker.id)
        await store.create(blocked)
        unblocked = Todo(title="unblocked", priority=Priority.HIGH)
        await store.create(unblocked)

        nxt = await store.next_todo(all_owners=True)
        assert nxt.title == "unblocked"

    @pytest.mark.asyncio
    async def test_skips_done(self, store):
        done = Todo(title="done", priority=Priority.CRITICAL)
        done.status = TodoStatus.DONE
        await store.create(done)
        medium = await store.create(Todo(title="medium", priority=Priority.MEDIUM))
        nxt = await store.next_todo(all_owners=True)
        assert nxt.title == "medium"

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, store):
        assert await store.next_todo(all_owners=True) is None

    @pytest.mark.asyncio
    async def test_oldest_wins_within_priority(self, store):
        old = Todo(title="old", priority=Priority.HIGH)
        old.created_at = now() - timedelta(hours=5)
        new = Todo(title="new", priority=Priority.HIGH)
        for t in [new, old]:
            await store.create(t)
        nxt = await store.next_todo(all_owners=True)
        assert nxt.title == "old"

    @pytest.mark.asyncio
    async def test_owner_scoping(self, store):
        await store.create(Todo(title="mine", priority=Priority.CRITICAL, owner="bot-a"))
        await store.create(Todo(title="theirs", priority=Priority.CRITICAL, owner="bot-b"))
        nxt = await store.next_todo(owner="bot-a")
        assert nxt is not None
        assert nxt.title == "mine"

    @pytest.mark.asyncio
    async def test_human_todos_visible_to_agent(self, store):
        """Human-created (owner=None) todos are visible to any agent."""
        human = await store.create(Todo(title="human task", priority=Priority.HIGH, owner=None))
        nxt = await store.next_todo(owner="any-agent")
        assert nxt is not None
        assert nxt.title == "human task"


# ---------------------------------------------------------------------------
# REST API via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_path):
    """TestClient wired to a fresh TodoStore."""
    from fastapi.testclient import TestClient
    from pyclopse.api.app import create_app
    from unittest.mock import MagicMock
    from pyclopse.todos.store import TodoStore as TS

    gw = MagicMock()
    gw.config.gateway.cors_origins = ["*"]
    gw._todo_store = TS(persist_path=str(tmp_path / "todos.json"))

    app = create_app(gw)
    return TestClient(app)


class TestTodosAPI:

    def test_list_empty(self, api_client):
        r = api_client.get("/api/v1/todos/")
        assert r.status_code == 200
        assert r.json()["todos"] == []

    def test_create_and_list(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Test todo", "priority": "high"})
        assert r.status_code == 201
        todo = r.json()["todo"]
        assert todo["title"] == "Test todo"
        assert todo["priority"] == "high"

        r2 = api_client.get("/api/v1/todos/")
        assert len(r2.json()["todos"]) == 1

    def test_get_by_id(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Get me"})
        tid = r.json()["todo"]["id"]
        r2 = api_client.get(f"/api/v1/todos/{tid}")
        assert r2.status_code == 200
        assert r2.json()["todo"]["id"] == tid

    def test_get_missing(self, api_client):
        r = api_client.get("/api/v1/todos/nope1234")
        assert r.status_code == 404

    def test_update(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Original"})
        tid = r.json()["todo"]["id"]
        r2 = api_client.patch(f"/api/v1/todos/{tid}", json={"title": "Updated", "priority": "critical"})
        assert r2.status_code == 200
        assert r2.json()["todo"]["title"] == "Updated"
        assert r2.json()["todo"]["priority"] == "critical"

    def test_mark_done(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Task"})
        tid = r.json()["todo"]["id"]
        r2 = api_client.post(f"/api/v1/todos/{tid}/mark", json={"status": "done", "notes": "completed"})
        assert r2.status_code == 200
        t = r2.json()["todo"]
        assert t["status"] == "done"
        assert t["notes"] == "completed"
        assert t["completed_at"] is not None

    def test_mark_invalid_status(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Task"})
        tid = r.json()["todo"]["id"]
        r2 = api_client.post(f"/api/v1/todos/{tid}/mark", json={"status": "unknown"})
        assert r2.status_code == 422

    def test_delete(self, api_client):
        r = api_client.post("/api/v1/todos/", json={"title": "Delete me"})
        tid = r.json()["todo"]["id"]
        r2 = api_client.delete(f"/api/v1/todos/{tid}")
        assert r2.status_code == 200
        assert r2.json()["deleted"] == tid
        r3 = api_client.get(f"/api/v1/todos/{tid}")
        assert r3.status_code == 404

    def test_next_todo(self, api_client):
        api_client.post("/api/v1/todos/", json={"title": "Low", "priority": "low"})
        api_client.post("/api/v1/todos/", json={"title": "Critical", "priority": "critical"})
        r = api_client.get("/api/v1/todos/next")
        assert r.status_code == 200
        assert r.json()["todo"]["title"] == "Critical"

    def test_next_todo_empty(self, api_client):
        r = api_client.get("/api/v1/todos/next")
        assert r.status_code == 200
        assert r.json()["todo"] is None

    def test_filter_by_status(self, api_client):
        api_client.post("/api/v1/todos/", json={"title": "Open task"})
        r_all = api_client.get("/api/v1/todos/")
        tid = r_all.json()["todos"][0]["id"]
        api_client.post(f"/api/v1/todos/{tid}/mark", json={"status": "done"})
        r = api_client.get("/api/v1/todos/?status=open")
        assert len(r.json()["todos"]) == 0
        r2 = api_client.get("/api/v1/todos/?status=done")
        assert len(r2.json()["todos"]) == 1


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

async def _pyclopse_session(home_dir: str, gateway_url: str = "http://localhost:19999"):
    env = {
        **os.environ,
        "HOME": home_dir,
        "PYCLAW_MCP_TRANSPORT": "stdio",
        "PYCLAW_EXEC_SECURITY": "all",
        "PYCLAW_GATEWAY_URL": gateway_url,
    }
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "pyclopse.tools.server"],
        env=env,
    )


async def _call(session: ClientSession, tool: str, args: dict) -> str:
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


@pytest.mark.asyncio
async def test_mcp_tools_registered(tmp_path):
    """Verify all TODO MCP tools are registered."""
    params = await _pyclopse_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            expected = {
                "todos_list", "todo_get", "todo_create",
                "todo_update", "todo_mark", "todo_delete", "todos_next",
            }
            assert expected.issubset(names), f"Missing: {expected - names}"


@pytest.mark.asyncio
async def test_mcp_todos_list_gateway_down(tmp_path):
    """todos_list returns [ERROR] when gateway is not running."""
    params = await _pyclopse_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "todos_list", {})
            assert "[ERROR]" in out


@pytest.mark.asyncio
async def test_mcp_todo_create_gateway_down(tmp_path):
    """todo_create returns [ERROR] when gateway is not running."""
    params = await _pyclopse_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "todo_create", {"title": "Test"})
            assert "[ERROR]" in out


@pytest.mark.asyncio
async def test_mcp_todos_next_gateway_down(tmp_path):
    params = await _pyclopse_session(str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            out = await _call(session, "todos_next", {})
            assert "[ERROR]" in out
