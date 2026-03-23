"""Tools catalog API routes — list MCP servers/tools available to agents."""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger("pyclaw.api.tools")

router = APIRouter()


def _get_gateway():
    """Retrieve the global gateway instance.

    Returns:
        Gateway: The active gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_tools():
    """Return the MCP servers configured per agent.

    Returns:
        Dict[str, Any]: ``{"agents": [...], "total_agents": int}`` where each
            entry describes one agent's MCP server list and tool profile.
    """
    gateway = _get_gateway()
    config = gateway.config

    agents_tools: List[Dict[str, Any]] = []
    agents_raw = config.agents.model_dump() if config.agents else {}

    for agent_id, agent_data in agents_raw.items():
        if not isinstance(agent_data, dict):
            continue
        mcp_servers = agent_data.get("mcp_servers") or []
        tools_cfg = agent_data.get("tools") or {}
        agents_tools.append({
            "agent_id": agent_id,
            "agent_name": agent_data.get("name", agent_id),
            "mcp_servers": mcp_servers,
            "tools_profile": tools_cfg.get("profile"),
            "tools_allow": tools_cfg.get("allow", []),
            "tools_deny": tools_cfg.get("deny", []),
        })

    return {"agents": agents_tools, "total_agents": len(agents_tools)}


@router.get("/debug", response_model=Dict[str, Any])
async def get_debug():
    """Return live FastAgent runner state for all agents — useful for troubleshooting.

    Returns:
        Dict[str, Any]: ``{"agents": {...}}`` mapping agent IDs to debug dicts
            that include display name, running state, base runner info, and
            per-session runner details. Returns ``{"error": ..., "agents": {}}``
            if the agent manager has not been initialised.
    """
    gateway = _get_gateway()
    agent_manager = getattr(gateway, "_agent_manager", None)
    if agent_manager is None:
        return {"error": "agent_manager not initialised", "agents": {}}

    agents_debug: Dict[str, Any] = {}
    agents = getattr(agent_manager, "agents", {}) or {}

    for agent_id, agent in agents.items():
        base_runner = getattr(agent, "fast_agent_runner", None)
        session_runners = getattr(agent, "_session_runners", {}) or {}

        def _runner_info(runner) -> Dict[str, Any]:
            """Serialise an AgentRunner instance to a debug-friendly dict.

            Args:
                runner: An AgentRunner instance, or None if not yet created.

            Returns:
                Dict[str, Any]: Runner metadata including initialisation state,
                    agent name, model, MCP server list, history path, and the
                    names of any FastAgent agents registered on the runner's app.
                    Returns ``{"initialised": False}`` when runner is None.
            """
            if runner is None:
                return {"initialised": False}
            fa_agent_names: List[str] = []
            app = getattr(runner, "_app", None)
            if app is not None:
                try:
                    fa_agent_names = list(getattr(app, "_agents", {}).keys())
                except Exception:
                    pass
                if not fa_agent_names:
                    try:
                        fa_agent_names = list(getattr(app, "agents", {}).keys())
                    except Exception:
                        pass
            return {
                "initialised": app is not None,
                "agent_name": runner.agent_name,
                "owner_name": runner.owner_name,
                "model": runner.model,
                "servers": runner.servers,
                "history_path": str(runner.history_path) if runner.history_path else None,
                "fa_agent_names": fa_agent_names,
            }

        agents_debug[agent_id] = {
            "display_name": getattr(agent, "name", agent_id),
            "is_running": getattr(agent, "is_running", False),
            "base_runner": _runner_info(base_runner),
            "session_runner_count": len(session_runners),
            "session_runners": {
                sid: _runner_info(r) for sid, r in session_runners.items()
            },
        }

    return {"agents": agents_debug}
