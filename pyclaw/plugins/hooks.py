"""Hook system for pyclaw plugins."""

import logging
from typing import Any, Callable, Dict, List
from enum import Enum
from dataclasses import dataclass, field


logger = logging.getLogger("pyclaw.plugins")


class HookPhase(str, Enum):
    """Phases where hooks can be registered."""
    BEFORE_GATEWAY_START = "before_gateway_start"
    AFTER_GATEWAY_START = "after_gateway_start"
    BEFORE_GATEWAY_STOP = "before_gateway_stop"
    AFTER_GATEWAY_STOP = "after_gateway_stop"
    
    BEFORE_AGENT_START = "before_agent_start"
    AFTER_AGENT_RESPONSE = "after_agent_response"
    
    BEFORE_TOOL_EXEC = "before_tool_exec"
    AFTER_TOOL_EXEC = "after_tool_exec"
    
    ON_MESSAGE = "on_message"
    ON_MESSAGE_SEND = "on_message_send"
    
    PLUGIN_LOAD = "plugin_load"
    PLUGIN_UNLOAD = "plugin_unload"


HookHandler = Callable[..., Any]


@dataclass
class HookRegistration:
    """Registration of a hook handler."""
    phase: HookPhase
    handler: HookHandler
    priority: int = 0  # Lower = runs first
    description: str = ""


class HookRegistry:
    """
    Registry for managing hook handlers.
    
    Hooks allow plugins to inject logic at various points
    in the gateway lifecycle.
    """
    
    def __init__(self):
        self._hooks: Dict[HookPhase, List[HookRegistration]] = {
            phase: [] for phase in HookPhase
        }
        self._global_handlers: List[HookRegistration] = []
    
    def register(
        self,
        phase: HookPhase,
        handler: HookHandler,
        priority: int = 0,
        description: str = "",
    ) -> None:
        """
        Register a hook handler.
        
        Args:
            phase: Phase to hook into
            handler: Async function to call
            priority: Lower = runs first
            description: Optional description
        """
        registration = HookRegistration(
            phase=phase,
            handler=handler,
            priority=priority,
            description=description,
        )
        
        self._hooks[phase].append(registration)
        self._hooks[phase].sort(key=lambda r: r.priority)
        
        logger.debug(f"Registered hook: {phase} -> {handler.__name__}")
    
    def register_global(
        self,
        handler: HookHandler,
        priority: int = 0,
    ) -> None:
        """
        Register a handler that runs on all phases.
        
        Args:
            handler: Async function to call
            priority: Lower = runs first
        """
        registration = HookRegistration(
            phase=None,  # Global
            handler=handler,
            priority=priority,
        )
        
        self._global_handlers.append(registration)
        self._global_handlers.sort(key=lambda r: r.priority)
    
    async def run(
        self,
        phase: HookPhase,
        context: Dict[str, Any],
    ) -> None:
        """
        Run all handlers for a phase.
        
        Args:
            phase: Phase to trigger
            context: Data to pass to handlers
        """
        # Run global handlers first
        for reg in self._global_handlers:
            try:
                await self._safe_call(reg.handler, phase, context)
            except Exception as e:
                logger.error(f"Global hook error in {reg.handler.__name__}: {e}")
        
        # Run phase-specific handlers
        for reg in self._hooks.get(phase, []):
            try:
                await self._safe_call(reg.handler, phase, context)
            except Exception as e:
                logger.error(f"Hook error in {reg.handler.__name__}: {e}")
    
    async def _safe_call(
        self,
        handler: HookHandler,
        phase: HookPhase,
        context: Dict[str, Any],
    ) -> None:
        """Safely call a hook handler."""
        try:
            # Try async first
            if asyncio.iscoroutinefunction(handler):
                await handler(context)
            else:
                # Fall back to sync
                handler(context)
        except Exception as e:
            logger.error(f"Hook {handler.__name__} failed for {phase}: {e}")
            raise
    
    def list_hooks(self) -> Dict[str, List[str]]:
        """List all registered hooks."""
        result = {}
        for phase, regs in self._hooks.items():
            if regs:
                result[phase.value] = [
                    f"{r.handler.__name__} (p={r.priority})"
                    for r in regs
                ]
        return result
    
    def clear(self) -> None:
        """Clear all hooks."""
        for phase in HookPhase:
            self._hooks[phase].clear()
        self._global_handlers.clear()


import asyncio


__all__ = ["HookPhase", "HookHandler", "HookRegistry"]
