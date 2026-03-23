# Conventions

Code and architecture patterns used throughout pyclaw. Follow these when adding
features to keep the codebase consistent.

---

## Config

- YAML keys are **camelCase**; Python attributes are **snake_case**
- Always use `validation_alias=AliasChoices("snake_case", "camelCase")` for new fields
- Default values go in the Python model, not in YAML templates
- Never hardcode paths — use `Path("~/.pyclaw/...").expanduser()`
- Test with `Model.model_validate({"camelCaseKey": val})`, not the constructor

---

## MCP Tools

- Tools are thin wrappers: call REST API → return string
- Return strings, not dicts — the agent reads text
- 404 responses: use `_fmt_http_err(e, resource_id)` → `"[NOT FOUND] ..."`
- Keep tool docstrings as the agent's primary documentation — be specific about
  args and include examples
- Tool names use snake_case with a domain prefix: `memory_store`, `job_create`
- Never import from `pyclaw.core.gateway` inside a tool — go through the REST API

---

## Async Patterns

- All I/O is async. Use `await` and `async def` throughout.
- Background tasks: `asyncio.create_task(coro(), name="descriptive-name")`
- Task names are logged in errors — always provide them
- For shutdown: cancel task, await it, catch `asyncio.CancelledError`
- Never `asyncio.sleep(0)` to yield — use proper awaits

---

## Error Handling

- Catch specific exceptions, not bare `except Exception` in hot paths
- Log with `self._logger.error(f"...: {e}", exc_info=True)` for unexpected errors
- Return friendly strings to agents, never raw tracebacks
- On agent runner error: call `agent.evict_session_runner(session_id)` before
  returning the error response so the next message gets a clean runner

---

## File I/O

- Atomic writes: write to `.tmp` file, then `os.replace()` to final path
- History files: always use `fast_agent.mcp.prompt_serialization.save_messages()`
  and `load_messages()` — never write FA JSON manually
- All data under `~/.pyclaw/` — never write elsewhere without config override

---

## Logging

- Logger names follow the module hierarchy: `pyclaw.core.gateway`,
  `pyclaw.agent.{agent_id}`, `pyclaw.self`, etc.
- Per-agent loggers: `logging.getLogger(f"pyclaw.agent.{agent_id}")`
- Don't use `print()` in library code — use loggers
- `print()` is acceptable only in `__main__.py` for startup messages

---

## Imports

- No circular imports: `pyclaw.api.routes.*` imports from `pyclaw.api.app`
  using a deferred `_get_gateway()` function, not a module-level import
- `pyclaw.tools.server` should not import from `pyclaw.core.gateway` — use REST
- New modules: add to the appropriate `__init__.py` only if re-exporting is needed

---

## Naming

| Thing | Convention | Example |
|-------|-----------|---------|
| Classes | PascalCase | `AgentRunner`, `DocLoader` |
| Functions/methods | snake_case | `run_stream`, `self_topics` |
| Private attributes | `_` prefix | `_session_runners`, `_mcp_task` |
| Constants | UPPER_SNAKE | `_KNOWLEDGE_DIR`, `_REPO_SSH` |
| MCP tool functions | snake_case, domain prefix | `memory_store`, `job_create` |
| Test files | `test_` prefix | `test_self_loader.py` |
| Route files | domain name | `sessions.py`, `self_.py` |

---

## FastMCP Rules

- **Never** use `mcp.http_app()` + manual uvicorn. Always `mcp.run_http_async()`.
- FastMCP is the **only** MCP library used. Do not import from the low-level `mcp` SDK.
- MCP servers are stopped by cancelling the asyncio task, not via any FastMCP API.
- Suppress noisy loggers before cancel; restore after.
