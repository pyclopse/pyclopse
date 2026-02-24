"""Pulse runner - async polling system for agents."""
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Callable, Awaitable
from contextlib import AsyncExitStack

from .models import PulseTask, PulseResult, PulseStatus, PulseActiveHours

logger = logging.getLogger("pyclaw.pulse")


class PulseRunner:
    """
    Async-native pulse runner that polls agents at configurable intervals.
    
    Each agent runs in its own async task - NO global lock!
    This avoids OpenClaw's concurrency issues.
    """
    
    def __init__(
        self,
        agent_executor: Optional[Callable[[str, str], Awaitable[str]]] = None,
    ):
        """
        Args:
            agent_executor: Async function(agent_id, prompt) -> response_message
        """
        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_configs: Dict[str, PulseTask] = {}
        self._running = False
        self._exit_stack: Optional[AsyncExitStack] = None
        self._agent_executor = agent_executor
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    def register_task(self, task: PulseTask) -> None:
        """Register a pulse task."""
        self._task_configs[task.agent_id] = task
        logger.info(
            f"Registered pulse task for agent '{task.agent_id}' "
            f"every {task.interval_seconds}s"
        )
    
    def register_tasks(self, tasks: List[PulseTask]) -> None:
        """Register multiple pulse tasks."""
        for task in tasks:
            self.register_task(task)
    
    def unregister_task(self, agent_id: str) -> None:
        """Unregister a pulse task."""
        if agent_id in self._tasks:
            self._tasks[agent_id].cancel()
            del self._tasks[agent_id]
        if agent_id in self._task_configs:
            del self._task_configs[agent_id]
        logger.info(f"Unregistered pulse task for agent '{agent_id}'")
    
    async def start(self) -> None:
        """Start the pulse runner."""
        if self._running:
            logger.warning("Pulse runner already running")
            return
        
        self._running = True
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        
        # Start each registered task in its own async task
        for agent_id, task in self._task_configs.items():
            if task.enabled:
                self._tasks[agent_id] = asyncio.create_task(
                    self._run_pulse_loop(agent_id),
                    name=f"pulse-{agent_id}"
                )
                logger.info(f"Started pulse loop for agent '{agent_id}'")
    
    async def stop(self) -> None:
        """Stop the pulse runner."""
        if not self._running:
            return
        
        self._running = False
        
        # Cancel all running tasks
        for agent_id, task in self._tasks.items():
            if not task.done():
                task.cancel()
                logger.info(f"Cancelled pulse task for agent '{agent_id}'")
        
        # Wait for tasks to finish
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        
        self._tasks.clear()
        
        if self._exit_stack:
            await self._exit_stack.__aexit__(None, None, None)
            self._exit_stack = None
        
        logger.info("Pulse runner stopped")
    
    async def _run_pulse_loop(self, agent_id: str) -> None:
        """
        Run the pulse loop for a single agent.
        Each agent runs independently - no global lock!
        """
        task = self._task_configs.get(agent_id)
        if not task:
            return
        
        # Initial delay to stagger startup
        await asyncio.sleep(1)
        
        while self._running and agent_id in self._task_configs:
            task = self._task_configs[agent_id]
            
            # Check if task is still enabled
            if not task.enabled:
                await asyncio.sleep(task.interval_seconds)
                continue
            
            result = await self._execute_pulse(task)
            
            # Log result
            if result.is_success:
                logger.debug(
                    f"Pulse for agent '{agent_id}' completed: {result.message}"
                )
            else:
                logger.warning(
                    f"Pulse for agent '{agent_id}' {result.status.value}: "
                    f"{result.error or result.message}"
                )
            
            # Sleep for the configured interval
            await asyncio.sleep(task.interval_seconds)
    
    async def _execute_pulse(self, task: PulseTask) -> PulseResult:
        """Execute a single pulse for a task."""
        start_time = datetime.now()
        
        # Check active hours
        if task.active_hours and not task.active_hours.is_active():
            return PulseResult(
                task=task,
                status=PulseStatus.SKIPPED,
                message="Outside active hours",
            )
        
        # Execute pulse if executor is available
        if self._agent_executor:
            try:
                response = await asyncio.wait_for(
                    self._agent_executor(task.agent_id, task.prompt),
                    timeout=300.0  # 5 minute timeout
                )
                duration_ms = int(
                    (datetime.now() - start_time).total_seconds() * 1000
                )
                
                return PulseResult(
                    task=task,
                    status=PulseStatus.SUCCESS,
                    message=response,
                    duration_ms=duration_ms,
                )
            except asyncio.TimeoutError:
                return PulseResult(
                    task=task,
                    status=PulseStatus.TIMEOUT,
                    error="Pulse execution timed out after 300s",
                    duration_ms=int(
                        (datetime.now() - start_time).total_seconds() * 1000
                    ),
                )
            except Exception as e:
                return PulseResult(
                    task=task,
                    status=PulseStatus.FAILURE,
                    error=str(e),
                    duration_ms=int(
                        (datetime.now() - start_time).total_seconds() * 1000
                    ),
                )
        else:
            # No executor - simulate success
            return PulseResult(
                task=task,
                status=PulseStatus.SUCCESS,
                message="No executor configured (mock pulse)",
            )
    
    def get_task_status(self, agent_id: str) -> Optional[Dict]:
        """Get status of a pulse task."""
        task = self._task_configs.get(agent_id)
        if not task:
            return None
        
        running_task = self._tasks.get(agent_id)
        
        return {
            "agent_id": agent_id,
            "enabled": task.enabled,
            "interval_seconds": task.interval_seconds,
            "active_hours": (
                {"start": task.active_hours.start, "end": task.active_hours.end}
                if task.active_hours else None
            ),
            "running": running_task is not None and not running_task.done(),
        }
    
    def list_tasks(self) -> List[Dict]:
        """List all registered pulse tasks."""
        return [
            self.get_task_status(agent_id)
            for agent_id in self._task_configs
        ]
