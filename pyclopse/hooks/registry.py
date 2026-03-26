"""Unified hook registry supporting both Python and file-based handlers."""

import asyncio
from pyclopse.reflect import reflect_system
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .events import HookEvent

logger = logging.getLogger("pyclopse.hooks")


@dataclass
class HookRegistration:
    """A single registered hook handler entry stored in the HookRegistry.

    Attributes:
        event (str): Event name this handler is registered for.
        handler (Callable): Async callable invoked when the event fires.
        priority (int): Execution order; lower values run first. Defaults to 0.
        description (str): Human-readable description for introspection. Defaults to "".
        source (str): Origin of the handler — "code" for Python handlers or
            "file:<path>" for subprocess-backed file handlers. Defaults to "code".
    """

    event: str
    handler: Callable
    priority: int = 0       # lower = runs first
    description: str = ""
    source: str = "code"    # "code" | "file:<path>"


@reflect_system("hooks")
class HookRegistry:
    """
    Unified hook registry.

    Supports two handler contracts:

    Notification hooks (everything except memory:*)
        All registered handlers are called.  Return values are ignored.
        Exceptions are caught and logged; they do not stop the chain.

    Interceptable hooks (memory:*)
        Handlers run in priority order.  The first handler that returns a
        non-None value wins — subsequent handlers are skipped and the value
        is returned to the caller.  If no handler returns a value, the
        caller falls back to the default backend.

    Both Python async callables and subprocess-backed file handlers use
    the same registration API; the subprocess wrapping is done by
    HookLoader before registration.
    """

    def __init__(self) -> None:
        """Initialise an empty HookRegistry with no registered handlers."""
        self._hooks: Dict[str, List[HookRegistration]] = {}

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register(
        self,
        event: str,
        handler: Callable,
        priority: int = 0,
        description: str = "",
        source: str = "code",
    ) -> None:
        """
        Register a handler for an event.

        Args:
            event:       Event name, e.g. "gateway:startup" or "memory:read".
            handler:     Async callable ``async def handler(context: dict) -> Any``.
            priority:    Execution order; lower numbers run first (default 0).
            description: Human-readable description for listing.
            source:      "code" for Python handlers, "file:<path>" for file-based.
        """
        reg = HookRegistration(
            event=event,
            handler=handler,
            priority=priority,
            description=description,
            source=source,
        )
        bucket = self._hooks.setdefault(event, [])
        bucket.append(reg)
        bucket.sort(key=lambda r: r.priority)
        logger.debug(f"Registered hook: {event} -> {handler.__name__} (p={priority})")

    def unregister(self, event: str, handler: Callable) -> bool:
        """Remove a specific handler from an event's registration list.

        Uses identity comparison (``is``) to locate the handler.

        Args:
            event (str): Event name to remove the handler from.
            handler (Callable): The exact callable object to remove.

        Returns:
            bool: True if the handler was found and removed; False otherwise.
        """
        bucket = self._hooks.get(event, [])
        before = len(bucket)
        self._hooks[event] = [r for r in bucket if r.handler is not handler]
        return len(self._hooks[event]) < before

    def clear(self, event: Optional[str] = None) -> None:
        """Clear registered handlers for a specific event or for all events.

        Args:
            event (Optional[str]): Event name to clear. If None, all handlers
                for all events are removed. Defaults to None.
        """
        if event:
            self._hooks.pop(event, None)
        else:
            self._hooks.clear()

    # ------------------------------------------------------------------ #
    # Firing
    # ------------------------------------------------------------------ #

    async def notify(self, event: str, context: Dict[str, Any]) -> None:
        """
        Fire all handlers for a notification event.

        All handlers run regardless of return value.  Exceptions are caught
        and logged but do not stop the chain.  Also fires ``command:*``
        handlers for any ``command:`` event.
        """
        handlers = list(self._hooks.get(event, []))

        # Wildcard: fire command:* for every command:xxx event
        if event.startswith("command:") and event != HookEvent.COMMAND_ANY:
            handlers = handlers + list(self._hooks.get(HookEvent.COMMAND_ANY, []))
            handlers.sort(key=lambda r: r.priority)

        for reg in handlers:
            try:
                result = reg.handler(context)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(
                    f"Hook error [{event}] in {reg.handler.__name__}: {exc}",
                    exc_info=True,
                )

    async def intercept(
        self,
        event: str,
        context: Dict[str, Any],
        default: Any = None,
    ) -> Any:
        """
        Fire handlers in priority order; return the first non-None result.

        If no handler returns a value, ``default`` is returned.
        """
        for reg in self._hooks.get(event, []):
            try:
                result = reg.handler(context)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    return result
            except Exception as exc:
                logger.error(
                    f"Hook error [{event}] in {reg.handler.__name__}: {exc}",
                    exc_info=True,
                )
        return default

    async def run(
        self,
        event: str,
        context: Dict[str, Any],
        default: Any = None,
    ) -> Any:
        """
        Dispatch to notify() or intercept() based on the event type.

        Interceptable events (memory:*) use intercept(); everything else
        uses notify() and returns None.
        """
        if event in HookEvent.INTERCEPTABLE:
            return await self.intercept(event, context, default=default)
        await self.notify(event, context)
        return None

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def list_hooks(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return a mapping of event names to their registered handler info dicts.

        Only events with at least one handler are included. Each handler is
        represented as a dict with keys: ``name``, ``priority``, ``description``,
        and ``source``.

        Returns:
            Dict[str, List[Dict[str, Any]]]: Mapping of event name to list of
                handler info dicts, suitable for the ``/api/v1/hooks`` endpoint.
        """
        result: Dict[str, List[Dict[str, Any]]] = {}
        for event, regs in self._hooks.items():
            if regs:
                result[event] = [
                    {
                        "name": reg.handler.__name__,
                        "priority": reg.priority,
                        "description": reg.description,
                        "source": reg.source,
                    }
                    for reg in regs
                ]
        return result

    def event_count(self) -> int:
        """Return the number of events that have at least one registered handler.

        Returns:
            int: Count of distinct event names with one or more handlers.
        """
        return sum(1 for regs in self._hooks.values() if regs)

    def handler_count(self) -> int:
        """Return the total number of registered handlers across all events.

        Returns:
            int: Sum of handler counts for every event in the registry.
        """
        return sum(len(regs) for regs in self._hooks.values())
