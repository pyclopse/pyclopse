"""Tools catalog API routes — list MCP servers/tools available to agents."""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger("pyclaw.api.tools")

router = APIRouter()


def _get_gateway():
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_tools():
    """Return the MCP servers configured per agent."""
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
