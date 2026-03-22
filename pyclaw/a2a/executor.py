"""A2A AgentExecutor that bridges incoming A2A tasks to pyclaw agents."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.utils import new_agent_text_message

if TYPE_CHECKING:
    from a2a.server.events import EventQueue

logger = logging.getLogger(__name__)


class PyclawAgentExecutor(AgentExecutor):
    """Routes an incoming A2A task message through the pyclaw gateway.

    Each inbound A2A request is routed through ``gateway.handle_message()``
    using ``channel="a2a"``, which means it lands in the agent's single active
    session — giving the agent full conversation context (same session as
    Telegram/Slack).  The A2A ``context_id`` is used as the sender_id so that
    messages within the same A2A conversation context share session state.
    """

    def __init__(self, agent_id: str, gateway: Any) -> None:
        self._agent_id = agent_id
        self._gateway = gateway

    async def execute(
        self,
        context: RequestContext,
        event_queue: "EventQueue",
    ) -> None:
        message = context.get_user_input().strip()
        if not message:
            return

        # Use context_id (A2A conversation context) as sender_id so multiple
        # turns within the same A2A context share the agent's active session.
        sender_id = context.context_id or context.task_id or "a2a"

        try:
            response = await self._gateway.handle_message(
                channel="a2a",
                sender="a2a-client",
                sender_id=str(sender_id),
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
