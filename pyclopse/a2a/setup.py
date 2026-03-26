"""Mount per-agent A2A endpoints onto the existing FastAPI app."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyclopse.core.gateway import Gateway

logger = logging.getLogger(__name__)


def mount_a2a_routes(gateway: "Gateway", fastapi_app: Any) -> int:
    """Add A2A JSON-RPC + agent-card routes to *fastapi_app* for each A2A-enabled agent.

    Routes added per agent (agent_id = e.g. "ritchie"):
        GET  /a2a/{agent_id}/.well-known/agent.json   → A2A agent card
        POST /a2a/{agent_id}/                          → JSON-RPC (tasks/send etc.)
        GET  /a2a/{agent_id}/agent/authenticatedExtendedCard

    Returns the number of agents successfully mounted.
    Safe to call multiple times only if the FastAPI app hasn't started serving yet
    (dynamically adding routes after startup works but is not officially supported by FastAPI).
    """
    try:
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.types import (
            AgentCard,
            AgentCapabilities,
            AgentSkill,
            TransportProtocol,
        )
    except ImportError:
        logger.warning("a2a-sdk not installed — A2A endpoints disabled (pip install a2a-sdk)")
        return 0

    from pyclopse.a2a.executor import PyclawAgentExecutor

    am = getattr(gateway, "_agent_manager", None)
    if not am:
        logger.warning("A2A: agent manager not available, skipping mount")
        return 0

    gw_cfg = getattr(gateway, "_config", None)

    # Global A2A enabled check
    global_a2a = None
    try:
        global_a2a = gw_cfg.gateway.a2a if gw_cfg else None
    except Exception:
        pass
    if global_a2a is not None and not getattr(global_a2a, "enabled", False):
        logger.info("A2A: disabled globally (gateway.a2a.enabled=false)")
        return 0

    # Derive API base URL
    api_port = 8080
    try:
        api_port = gw_cfg.gateway.port or 8080
    except Exception:
        pass
    api_host = "localhost"

    # Pyclaw version for the card
    try:
        from pyclopse import __version__ as _pyclopse_ver
    except Exception:
        _pyclopse_ver = "0.0.0"

    mounted = 0
    for agent_id, agent in am.agents.items():
        # Per-agent A2A config
        agent_a2a = getattr(agent.config, "a2a", None)
        if agent_a2a is not None:
            if not getattr(agent_a2a, "enabled", True):
                logger.debug(f"A2A: agent '{agent_id}' disabled")
                continue
            if not getattr(agent_a2a, "allow_inbound", True):
                logger.debug(f"A2A: agent '{agent_id}' allow_inbound=false, skipping")
                continue
        # If no per-agent config AND global A2A is not explicitly enabled, skip
        elif global_a2a is None or not getattr(global_a2a, "enabled", False):
            continue

        try:
            agent_url = f"http://{api_host}:{api_port}/a2a/{agent_id}/"
            description = getattr(agent.config, "description", "") or f"pyclopse agent: {agent_id}"

            # Build capabilities
            has_telegram = bool(getattr(gateway, "_tg_app", None) or getattr(gateway, "_telegram_bot", None))
            has_slack = bool(getattr(gateway, "_slack_web_client", None))
            capabilities = AgentCapabilities(
                streaming=False,
                push_notifications=has_telegram or has_slack,
                state_transition_history=False,
            )

            # Build skills from the skills registry
            a2a_skills: list[AgentSkill] = []
            try:
                from pyclopse.skills.registry import discover_skills
                extra_dirs = list(getattr(agent.config, "skills_dirs", None) or [])
                if gw_cfg and gw_cfg.gateway:
                    for d in (getattr(gw_cfg.gateway, "skills_dirs", None) or []):
                        if d not in extra_dirs:
                            extra_dirs.append(d)
                skills = discover_skills(
                    agent_name=agent_id,
                    config_dir="~/.pyclopse",
                    extra_dirs=extra_dirs or None,
                )
                for s in sorted(skills, key=lambda x: x.name.lower()):
                    skill_desc = ""
                    try:
                        content = s.read_content()
                        for line in content.splitlines():
                            if line.strip().startswith("description:"):
                                skill_desc = line.split(":", 1)[1].strip()
                                break
                    except Exception:
                        pass
                    a2a_skills.append(AgentSkill(
                        id=f"skill:{s.name}",
                        name=s.name,
                        description=skill_desc,
                        tags=["skill"],
                    ))
            except Exception as e:
                logger.debug(f"A2A skills build error for '{agent_id}': {e}")

            agent_card = AgentCard(
                name=agent_id,
                description=description,
                url=agent_url,
                version=_pyclopse_ver,
                capabilities=capabilities,
                skills=a2a_skills,
                default_input_modes=["text/plain"],
                default_output_modes=["text/plain"],
                preferred_transport=TransportProtocol.jsonrpc,
            )

            session_mode = getattr(agent_a2a, "session_mode", "shared") if agent_a2a else "shared"
            executor = PyclawAgentExecutor(agent_id, gateway, session_mode=session_mode)
            handler = DefaultRequestHandler(
                agent_executor=executor,
                task_store=InMemoryTaskStore(),
            )
            a2a_starlette = A2AStarletteApplication(
                agent_card=agent_card,
                http_handler=handler,
            )
            a2a_starlette.add_routes_to_app(
                fastapi_app,
                agent_card_url=f"/a2a/{agent_id}/.well-known/agent.json",
                rpc_url=f"/a2a/{agent_id}/",
                extended_agent_card_url=f"/a2a/{agent_id}/agent/authenticatedExtendedCard",
            )
            mounted += 1
            logger.info(f"A2A: mounted agent '{agent_id}' at /a2a/{agent_id}/ (card: /a2a/{agent_id}/.well-known/agent.json)")

        except Exception as e:
            logger.error(f"A2A: failed to mount agent '{agent_id}': {e}", exc_info=True)

    if mounted:
        logger.info(f"A2A: {mounted} agent(s) exposed")
    else:
        logger.debug("A2A: no agents mounted (check gateway.a2a.enabled or per-agent a2a config)")
    return mounted
