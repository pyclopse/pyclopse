"""pyclaw reflection system.

Decorators that annotate classes and functions as named systems, events, and
commands. A global registry accumulates entries at import time; the ``reflect``
MCP tool queries it so agents can explore pyclaw's own architecture live.

Usage::

    from pyclaw.reflect import reflect_system, reflect_event, reflect_command

    @reflect_system("jobs")
    class JobScheduler:
        \"\"\"...\"\"\"

    @reflect_event("job:complete")
    class _JobCompleteEvent:
        \"\"\"Payload: {job_id, result, agent_id}\"\"\"

    @reflect_command("/reset")
    async def cmd_reset(args, ctx):
        \"\"\"...\"\"\"
"""

from .decorators import reflect_system, reflect_event, reflect_command
from .registry import get_registry, query


@reflect_system("reflection")
class _ReflectionSystem:
    """Live architecture reflection system for pyclaw.

    Decorators annotate classes and functions at import time so agents can
    explore pyclaw's structure without reading static documentation.

    **Decorators:**

    - ``@reflect_system("name")`` — a major pyclaw subsystem (class or sentinel)
    - ``@reflect_event("name")`` — a hook event namespace class
    - ``@reflect_command("name")`` — a slash command handler function

    Multiple objects decorated with the same ``(category, name)`` pair have
    their docstrings merged and sorted by source location.

    **MCP tools (in pyclaw/tools/server.py):**

    - ``reflect(category?, name?)`` — query the registry
    - ``reflect_source(module)`` — read source with line numbers

    **Query API:**

        reflect()                              → pyclaw.__doc__ overview
        reflect(category="system")             → list all systems
        reflect(category="system", name="X")   → system X detail + merged docstring
        reflect(category="event")              → list all events
        reflect(category="command")            → list all commands
        reflect(category="config", name="X")   → config section X Pydantic schema
        reflect(name="X")                      → cross-category name search

    **Registry population:** All entries are registered at import time.
    The gateway imports all subsystem modules during startup, so the full
    registry is available before any agent makes a tool call.
    """


del _ReflectionSystem  # sentinel only; no need to expose it


__all__ = [
    "reflect_system",
    "reflect_event",
    "reflect_command",
    "get_registry",
    "query",
]
