"""A2A AgentExecutor that bridges incoming A2A tasks to pyclawops agents."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from pyclawops.reflect import reflect_system

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.utils import new_agent_text_message

if TYPE_CHECKING:
    from a2a.server.events import EventQueue

logger = logging.getLogger(__name__)


@reflect_system("a2a")
class PyclawAgentExecutor(AgentExecutor):
    """Routes an incoming A2A task message through the pyclawops gateway.

    session_mode="shared" (default):
        Uses ``channel="a2a"`` → gateway's ``_get_active_session`` → the agent's
        single active session with full conversation context.

    session_mode="isolated":
        Uses ``channel="job"`` with the A2A task_id as sender_id →
        ``_get_or_create_session`` → a fresh session per A2A task, no prior context.
    """

    def __init__(self, agent_id: str, gateway: Any, session_mode: str = "shared") -> None:
        self._agent_id = agent_id
        self._gateway = gateway
        self._session_mode = session_mode

    async def execute(
        self,
        context: RequestContext,
        event_queue: "EventQueue",
    ) -> None:
        message = context.get_user_input().strip()
        if not message:
            return

        if self._session_mode == "isolated":
            # Each A2A task gets its own session — use task_id as the unique key.
            channel = "job"
            sender_id = str(context.task_id or "a2a-task")
        else:
            # Shared mode: route into the agent's active session (full context).
            # context_id groups multi-turn A2A conversations; falls back to task_id.
            channel = "a2a"
            sender_id = str(context.context_id or context.task_id or "a2a")

        try:
            response = await self._gateway.handle_message(
                channel=channel,
                sender="a2a-client",
                sender_id=sender_id,
                content=message,
                agent_id=self._agent_id,
            )
        except Exception as e:
            logger.error(f"A2A executor error for agent '{self._agent_id}': {e}")
            response = f"Error: {e}"

        if response:
            await event_queue.enqueue_event(new_agent_text_message(response))

    async def cancel(
        self,
        context: RequestContext,
        event_queue: "EventQueue",
    ) -> None:
        # Cancellation is not yet implemented; log and ignore.
        logger.debug(f"A2A cancel requested for agent '{self._agent_id}' task={context.task_id}")
