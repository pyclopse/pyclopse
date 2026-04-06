"""GatewayClient protocol — the abstraction layer between TUI and Gateway.

Two implementations exist:
  - EmbeddedGatewayClient: wraps a live in-process Gateway object
  - RemoteGatewayClient: connects via HTTP + WebSocket to a running gateway
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GatewayClient(Protocol):
    """Interface consumed by all TUI views."""

    # ── Agents ────────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return list of agent dicts with at least {id, name, model}."""
        ...

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        """Return full agent config as a dict of field→value pairs."""
        ...

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        """Return session dicts for an agent."""
        ...

    async def get_active_session(self, agent_id: str) -> dict[str, Any] | None:
        """Return the active session for an agent, or None."""
        ...

    async def get_session_history(self, agent_id: str, session_id: str) -> str:
        """Return raw JSON history content as a string."""
        ...

    # ── Messages ──────────────────────────────────────────────────────────────

    async def send_message(self, agent_id: str, content: str) -> None:
        """Send a user message to an agent (TUI channel)."""
        ...

    async def dispatch_command(self, agent_id: str, command: str) -> str | None:
        """Dispatch a slash command. Returns result text or None."""
        ...

    async def list_commands(self) -> list[dict[str, str]]:
        """Return list of {name, description} for all slash commands."""
        ...

    # ── Events ────────────────────────────────────────────────────────────────

    def subscribe_events(self, agent_id: str) -> asyncio.Queue:
        """Subscribe to real-time events for an agent. Returns an asyncio.Queue."""
        ...

    def unsubscribe_events(self, agent_id: str, queue: asyncio.Queue) -> None:
        """Unsubscribe a previously obtained event queue."""
        ...

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        """Return job dicts for an agent."""
        ...

    async def run_job(self, job_id: str) -> None:
        """Trigger a job to run immediately."""
        ...

    async def get_job_runs(self, agent_id: str, job_id: str) -> list[dict[str, Any]]:
        """Return run history for a job."""
        ...

    # ── Status ────────────────────────────────────────────────────────────────

    async def get_usage(self) -> dict[str, Any]:
        """Return gateway usage stats."""
        ...

    async def get_system_prompt(self, agent_id: str) -> str:
        """Return the reconstructed system prompt for an agent."""
        ...

    async def get_agent_log_tail(self, agent_id: str, lines: int = 500) -> str:
        """Return the last N lines of the agent log."""
        ...

    # ── Files ─────────────────────────────────────────────────────────────────

    async def list_agent_files(self, agent_id: str) -> list[dict[str, str]]:
        """Return list of {path, size, modified} dicts for agent data dir."""
        ...

    async def read_agent_file(self, agent_id: str, path: str) -> str:
        """Read a file from the agent's data directory."""
        ...

    async def write_agent_file(self, agent_id: str, path: str, content: str) -> None:
        """Write a file to the agent's data directory."""
        ...

    # ── Skills ────────────────────────────────────────────────────────────────

    async def list_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """Return skill dicts for an agent."""
        ...

    async def get_skill_content(self, agent_id: str, skill_name: str) -> str:
        """Return SKILL.md content for a skill."""
        ...

    # ── A2A ───────────────────────────────────────────────────────────────────

    async def get_agent_card(self, agent_id: str) -> dict[str, Any]:
        """Return A2A agent card dict."""
        ...

    # ── Misc ──────────────────────────────────────────────────────────────────

    async def get_show_thinking(self, agent_id: str) -> bool:
        """Return whether the agent shows thinking blocks."""
        ...
