"""Agent configuration step — add, edit, or remove agents."""

from typing import Any
from .. import menu


def _available_model_strings(config: dict) -> list[str]:
    """Build a list of provider/model strings from the current providers config."""
    strings = []
    for pid, pcfg in config.get("providers", {}).items():
        for model_id in pcfg.get("models", {}).keys():
            strings.append(f"{pid}/{model_id}")
    return strings


def _configure_single_agent(
    agent_id: str,
    existing: dict | None,
    config: dict,
) -> dict:
    """Interactively configure a single agent. Returns agent config dict."""
    ex = existing or {}

    # Display name
    default_name = ex.get("name", agent_id.capitalize())
    name = menu.ask("Display name", default=default_name)

    # Model
    model_strings = _available_model_strings(config)
    current_model = ex.get("model", "")
    menu.console.print()
    if model_strings:
        menu.info("  Available models from your providers:")
        for i, ms in enumerate(model_strings, 1):
            menu.info(f"    [bold]{i}[/bold]  {ms}")
        menu.console.print()
        default_choice = current_model if current_model in model_strings else (model_strings[0] if model_strings else "")
        raw = menu.ask("Model (provider/model-id)", default=default_choice)
        model = raw.strip() or default_choice
    else:
        menu.warn("No providers configured yet — enter model string manually.")
        model = menu.ask("Model (e.g. anthropic/claude-sonnet-4-6)", default=current_model)

    # MCP servers
    default_mcps = ex.get("mcp_servers", ["pyclopse", "fetch", "time", "filesystem"])
    mcp_raw = menu.ask(
        "MCP servers (comma-separated)",
        default=",".join(default_mcps),
    )
    mcp_servers = [s.strip() for s in mcp_raw.split(",") if s.strip()]

    # Show thinking
    show_thinking = ex.get("show_thinking", False)
    show_thinking = menu.confirm("Show thinking blocks to users?", default=show_thinking)

    # Build config
    cfg: dict[str, Any] = {
        "name": name,
        "model": model,
        "contextWindow": ex.get("contextWindow", 200000),
        "use_fastagent": True,
        "show_thinking": show_thinking,
        "mcp_servers": mcp_servers,
    }

    # Preserve any existing advanced keys (vault, queue, a2a, etc.)
    for k in ("vault", "queue", "a2a", "request_params", "tools", "skills_dirs", "max_iterations", "max_tokens"):
        if k in ex:
            cfg[k] = ex[k]

    return cfg


# ---------------------------------------------------------------------------
# Public step
# ---------------------------------------------------------------------------

def step_agents(config: dict, secrets: dict, env: dict) -> tuple[dict, dict, dict]:
    """Interactively configure agents section.

    Supports add, edit, and remove on top of any existing config.
    Returns updated (config, secrets, env).
    """
    menu.section("Agents")

    if "agents" not in config:
        config["agents"] = {}

    agents = config["agents"]

    while True:
        # Show current state
        if agents:
            menu.info("  Configured agents:")
            for aid, acfg in agents.items():
                menu.info(f"    [bold]{aid}[/bold]  {acfg.get('name', aid)}  —  {acfg.get('model', '?')}")
        else:
            menu.info("  No agents configured yet.")
        menu.console.print()

        options = [("add", "Add an agent")]
        if agents:
            options += [("edit", "Edit an existing agent")]
            options += [("remove", "Remove an agent")]
        options += [("done", "Done with agents")]

        action = menu.choose("Action", options, default="add" if not agents else "done")

        if action == "done":
            break

        elif action == "add":
            menu.section("Add Agent")
            agent_id_raw = menu.ask("Agent ID (e.g. main, assistant, coder)")
            agent_id = "".join(c for c in agent_id_raw.strip().lower().replace(" ", "_") if c.isalnum() or c == "_")
            if not agent_id:
                menu.warn("Invalid agent ID.")
                continue
            if agent_id in agents:
                menu.warn(f"Agent '{agent_id}' already exists — use Edit instead.")
                continue
            cfg = _configure_single_agent(agent_id, None, config)
            agents[agent_id] = cfg
            menu.success(f"Agent '{agent_id}' added.")

        elif action == "edit":
            aid_options = [(aid, f"{aid}  ({acfg.get('name', aid)})") for aid, acfg in agents.items()]
            aid = menu.choose("Which agent?", aid_options)
            menu.section(f"Edit Agent: {aid}")
            cfg = _configure_single_agent(aid, agents[aid], config)
            agents[aid] = cfg
            menu.success(f"Agent '{aid}' updated.")

        elif action == "remove":
            aid_options = [(aid, f"{aid}  ({acfg.get('name', aid)})") for aid, acfg in agents.items()]
            aid = menu.choose("Which agent to remove?", aid_options)
            if menu.confirm(f"Remove agent '{aid}'?", default=False):
                del agents[aid]
                menu.success(f"Agent '{aid}' removed.")

    config["agents"] = agents
    return config, secrets, env
