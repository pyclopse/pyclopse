# Testing

**Framework:** pytest with `asyncio_mode = "auto"` (all async tests run without
`@pytest.mark.asyncio`). Always run via `uv run pytest`.

---

## Running Tests

```bash
uv run pytest                          # all tests
uv run pytest tests/test_gateway.py   # one file
uv run pytest tests/test_commands.py::test_help_command -v  # one test
```

Never use `.venv/bin/pytest` or bare `python -m pytest`.

---

## Gateway Stub Pattern

The Gateway is never instantiated normally in unit tests. `__init__` does too
much (loads config, starts servers). Use the stub pattern instead:

```python
from pyclaw.core.gateway import Gateway

gw = Gateway.__new__(Gateway)           # skip __init__ entirely
gw._seen_message_ids = {}
gw._dedup_ttl_seconds = 60
gw._usage = {"messages": 0, "tokens": 0}

# Attach mocks as needed
from unittest.mock import MagicMock, AsyncMock
mock_sm = MagicMock()
mock_sm.get_active_session = AsyncMock(return_value=mock_session)
mock_sm.create_session     = AsyncMock(return_value=mock_session)
mock_sm.set_active_session = MagicMock()
gw._session_manager = mock_sm
```

---

## Config Validation Tests

Always use `model_validate` with camelCase keys. Never use keyword constructor:

```python
from pyclaw.config.schema import ExecApprovalsConfig, AgentConfig

# Correct
cfg = ExecApprovalsConfig.model_validate({"mode": "allowlist"})
agent = AgentConfig.model_validate({"name": "Test", "model": "sonnet"})

# Wrong — silently ignores fields
cfg = ExecApprovalsConfig(mode="allowlist")
```

---

## SessionManager in Tests

Use `agents_dir=str(tmp_path)` — never `persist_dir=`:

```python
from pyclaw.core.session import SessionManager

mgr = SessionManager(agents_dir=str(tmp_path), ttl_hours=1)
await mgr.start()
session = await mgr.create_session("agent1", "telegram", "user1")
mgr.set_active_session("agent1", session.id)

active = await mgr.get_active_session("agent1")
assert active.id == session.id
```

---

## AgentRunner Tests

Mock the concurrency manager to avoid semaphore deadlocks:

```python
from unittest.mock import patch, AsyncMock

with patch("pyclaw.core.concurrency.get_manager") as mock_mgr:
    mock_mgr.return_value.acquire = AsyncMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    result = await runner.run("test message")
```

For history I/O tests, patch `fast_agent.mcp.prompt_serialization`:
```python
with patch("fast_agent.mcp.prompt_serialization.load_messages") as mock_load, \
     patch("fast_agent.mcp.prompt_serialization.save_messages") as mock_save:
    ...
```

---

## Async Test Conventions

All tests use `asyncio_mode = "auto"` from `pyproject.toml`. Just write:

```python
async def test_my_thing():
    result = await some_async_function()
    assert result == expected
```

No `@pytest.mark.asyncio` needed. No manual event loop management.

---

## Fixtures

Common fixtures are in `tests/fixtures/`:
- `channel_plugins.py` — `EchoPlugin`, `AnotherPlugin` for channel tests

Test files use `tmp_path` (pytest built-in) for temporary directories.

---

## What NOT to Test

- FastMCP/FastAgent internals — test pyclaw's wrappers, not the libraries
- Config file I/O in unit tests — use `model_validate` directly
- Network calls — mock `httpx` or the specific API function
