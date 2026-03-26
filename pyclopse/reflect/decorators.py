"""Reflection decorators for pyclopse.

Pure side-effect decorators — each registers the target in the global registry
and returns it unchanged, so they are fully transparent to type checkers and
the runtime.

Usage::

    from pyclopse.reflect import reflect_system, reflect_event, reflect_command

    @reflect_system("gateway")
    class Gateway:
        \"\"\"The main orchestrator ...\"\"\"

    @reflect_event("job:complete")
    class _JobCompleteEvent:
        \"\"\"Payload: {job_id, result, agent_id}\"\"\"

    # For inner functions / closures:
    reflect_command("/reset")(cmd_reset_fn)
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from .registry import CATEGORY_COMMAND, CATEGORY_EVENT, CATEGORY_SYSTEM, register

T = TypeVar("T")


def reflect_system(name: str) -> Callable[[T], T]:
    """Decorator: register *obj* as a named pyclopse system."""

    def decorator(obj: T) -> T:
        register(CATEGORY_SYSTEM, name.lower(), obj)
        return obj

    return decorator


def reflect_event(name: str, payload: dict[str, Any] | None = None) -> Callable[[T], T]:
    """Decorator: register *obj* as a named pyclopse event.

    *payload* is an optional dict describing the event payload fields.
    """

    def decorator(obj: T) -> T:
        register(CATEGORY_EVENT, name.lower(), obj, payload=payload)
        return obj

    return decorator


def reflect_command(name: str) -> Callable[[T], T]:
    """Decorator: register *obj* as a named pyclopse slash command handler.

    Works as a decorator or a plain function call::

        @reflect_command("/help")
        async def cmd_help(args, ctx): ...

        # Inner-function / closure variant:
        reflect_command("/help")(cmd_help_fn)
    """

    def decorator(obj: T) -> T:
        register(CATEGORY_COMMAND, name.lower(), obj)
        return obj

    return decorator
