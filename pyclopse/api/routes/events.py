"""WebSocket endpoint for real-time event streaming to external TUI clients."""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("pyclopse.api.events")


def _get_gateway():
    from pyclopse.api.app import get_gateway
    return get_gateway()


@router.websocket("/ws/events/{agent_id}")
async def agent_events(websocket: WebSocket, agent_id: str):
    """Stream real-time agent events (stream_chunk, user_message, agent_response)
    over a WebSocket connection."""
    gateway = _get_gateway()
    await websocket.accept()

    queue = gateway.subscribe_agent(agent_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                # Send keepalive ping
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        logger.debug(f"WebSocket disconnected for agent {agent_id}")
    except Exception as e:
        logger.debug(f"WebSocket error for agent {agent_id}: {e}")
    finally:
        gateway.unsubscribe_agent(agent_id, queue)
