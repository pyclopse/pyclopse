"""RemoteGatewayClient — connects to a running gateway via HTTP + WebSocket.

Used when the TUI runs as a separate process from the gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("pyclopse.tui.remote_client")


class RemoteGatewayClient:
    """HTTP + WebSocket client that connects to a running pyclopse gateway."""

    def __init__(self, base_url: str = "http://localhost:8080") -> None:
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/api/v1"
        self._http = httpx.AsyncClient(base_url=self._base, timeout=30.0)
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self._ws_queues: dict[str, asyncio.Queue] = {}

    async def connect(self) -> None:
        """Verify the gateway is reachable."""
        try:
            r = await self._http.get("/health")
            r.raise_for_status()
        except Exception as e:
            raise ConnectionError(f"Cannot reach gateway at {self._base}: {e}") from e

    async def close(self) -> None:
        for task in self._ws_tasks.values():
            task.cancel()
        await self._http.aclose()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get(self, path: str, **params) -> Any:
        r = await self._http.get(f"{self._api}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, json_body: dict | None = None) -> Any:
        r = await self._http.post(f"{self._api}{path}", json=json_body or {})
        r.raise_for_status()
        return r.json()

    async def _put(self, path: str, json_body: dict | None = None) -> Any:
        r = await self._http.put(f"{self._api}{path}", json=json_body or {})
        r.raise_for_status()
        return r.json()

    # ── Agents ────────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict[str, Any]]:
        data = await self._get("/agents/")
        return data.get("agents", [])

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        return await self._get(f"/tui/agents/{agent_id}/config")

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/agents/{agent_id}/sessions")
        return data.get("sessions", [])

    async def get_active_session(self, agent_id: str) -> dict[str, Any] | None:
        data = await self._get(f"/tui/agents/{agent_id}/active-session")
        return data.get("session")

    async def get_session_history(self, agent_id: str, session_id: str) -> str:
        data = await self._get(f"/agents/{agent_id}/sessions/{session_id}/messages")
        return json.dumps(data, indent=2)

    # ── Messages ──────────────────────────────────────────────────────────────

    async def send_message(self, agent_id: str, content: str) -> None:
        await self._post(f"/agents/{agent_id}/messages", {
            "content": content,
            "channel": "tui",
            "sender": "tui_user",
            "sender_id": "tui_user",
        })

    async def dispatch_command(self, agent_id: str, command: str) -> str | None:
        data = await self._post("/commands/dispatch", {
            "command": command,
            "agent_id": agent_id,
        })
        return data.get("result")

    async def list_commands(self) -> list[dict[str, str]]:
        data = await self._get("/commands/")
        return data.get("commands", [])

    # ── Events ────────────────────────────────────────────────────────────────

    def subscribe_events(self, agent_id: str) -> asyncio.Queue:
        """Connect a WebSocket and return a local queue that receives events."""
        if agent_id in self._ws_queues:
            return self._ws_queues[agent_id]

        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._ws_queues[agent_id] = queue

        ws_url = self._base.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/v1/ws/events/{agent_id}"

        async def _reader():
            import websockets
            while True:
                try:
                    async with websockets.connect(ws_url) as ws:
                        async for msg in ws:
                            event = json.loads(msg)
                            if event.get("type") == "ping":
                                continue
                            try:
                                queue.put_nowait(event)
                            except asyncio.QueueFull:
                                pass
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.debug(f"WebSocket reconnect for {agent_id}: {e}")
                    await asyncio.sleep(1.0)

        self._ws_tasks[agent_id] = asyncio.create_task(_reader())
        return queue

    def unsubscribe_events(self, agent_id: str, queue: asyncio.Queue) -> None:
        task = self._ws_tasks.pop(agent_id, None)
        if task:
            task.cancel()
        self._ws_queues.pop(agent_id, None)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        data = await self._get("/jobs/", agent_id=agent_id)
        return data.get("jobs", [])

    async def run_job(self, job_id: str) -> None:
        await self._post(f"/jobs/{job_id}/run")

    async def get_job_runs(self, agent_id: str, job_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/jobs/{job_id}/runs")
        return data.get("runs", [])

    # ── Status ────────────────────────────────────────────────────────────────

    async def get_usage(self) -> dict[str, Any]:
        return await self._get("/usage/")

    async def get_system_prompt(self, agent_id: str) -> str:
        data = await self._get(f"/tui/agents/{agent_id}/system-prompt")
        return data.get("prompt", "")

    async def get_agent_log_tail(self, agent_id: str, lines: int = 500) -> str:
        data = await self._get(f"/tui/agents/{agent_id}/log-tail", lines=lines)
        return data.get("content", "")

    # ── Files ─────────────────────────────────────────────────────────────────

    async def list_agent_files(self, agent_id: str) -> list[dict[str, str]]:
        data = await self._get(f"/tui/agents/{agent_id}/files")
        return data.get("files", [])

    async def read_agent_file(self, agent_id: str, path: str) -> str:
        data = await self._get(f"/tui/agents/{agent_id}/files/content", path=path)
        return data.get("content", "")

    async def write_agent_file(self, agent_id: str, path: str, content: str) -> None:
        await self._put(f"/tui/agents/{agent_id}/files/content", {"content": content})

    # ── Skills ────────────────────────────────────────────────────────────────

    async def list_skills(self, agent_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/tui/agents/{agent_id}/skills")
        return data.get("skills", [])

    async def get_skill_content(self, agent_id: str, skill_name: str) -> str:
        data = await self._get(f"/tui/agents/{agent_id}/skills/{skill_name}/content")
        return data.get("content", "")

    # ── A2A ───────────────────────────────────────────────────────────────────

    async def get_agent_card(self, agent_id: str) -> dict[str, Any]:
        return await self._get(f"/tui/agents/{agent_id}/a2a-card")

    # ── Misc ──────────────────────────────────────────────────────────────────

    async def get_show_thinking(self, agent_id: str) -> bool:
        data = await self._get(f"/tui/agents/{agent_id}/show-thinking")
        return data.get("show_thinking", False)
