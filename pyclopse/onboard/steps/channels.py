"""Channel configuration step — Telegram, Slack, and more."""

from typing import Any
from .. import menu


def _agent_ids(config: dict) -> list[str]:
    return list(config.get("agents", {}).keys())


def _default_agent(config: dict) -> str:
    ids = _agent_ids(config)
    return ids[0] if ids else "main"


# ---------------------------------------------------------------------------
# Per-channel configurators
# ---------------------------------------------------------------------------

def _configure_telegram(existing: dict | None, config: dict, secrets: dict, env: dict) -> tuple[dict, dict, dict]:
    ex = existing or {"enabled": True, "streaming": True, "bots": {}}
    menu.section("Telegram")

    agent_ids = _agent_ids(config)
    bots: dict = ex.get("bots", {})

    while True:
        if bots:
            menu.info("  Configured bots:")
            for bname, bcfg in bots.items():
                menu.info(f"    [bold]{bname}[/bold]  →  agent: {bcfg.get('agent', '?')}")
        else:
            menu.info("  No bots configured yet.")
        menu.console.print()

        options = [("add", "Add a bot")]
        if bots:
            options += [("remove", "Remove a bot")]
        options += [("done", "Done")]
        action = menu.choose("Action", options, default="add" if not bots else "done")

        if action == "done":
            break

        elif action == "add":
            bot_name = menu.ask("Bot name (arbitrary label, e.g. main)", default="main")
            token = menu.ask("Bot token (from @BotFather)")
            if not token.strip():
                menu.warn("Token required.")
                continue

            if agent_ids:
                agent_options = [(a, a) for a in agent_ids]
                agent = menu.choose("Which agent handles this bot?", agent_options, default=_default_agent(config))
            else:
                agent = menu.ask("Agent ID", default="main")

            key_name = f"TELEGRAM_BOT_TOKEN_{bot_name.upper()}" if len(bots) > 0 else "TELEGRAM_BOT_TOKEN"
            env[key_name] = token.strip()
            secrets[key_name] = {"source": "env"}
            bots[bot_name] = {"botToken": f"${{{key_name}}}", "agent": agent}
            menu.success(f"Bot '{bot_name}' added.")

        elif action == "remove":
            opts = [(n, n) for n in bots]
            name = menu.choose("Remove which bot?", opts)
            if menu.confirm(f"Remove bot '{name}'?", default=False):
                del bots[name]

    streaming = ex.get("streaming", True)
    streaming = menu.confirm("Enable streaming (chunk-by-chunk responses)?", default=streaming)

    cfg = {"enabled": True, "streaming": streaming, "bots": bots}
    return cfg, secrets, env


def _configure_slack(existing: dict | None, config: dict, secrets: dict, env: dict) -> tuple[dict, dict, dict]:
    ex = existing or {}
    menu.section("Slack")

    menu.info("  You need a Bot Token (xoxb-...) and App Token (xapp-...) from api.slack.com")
    menu.console.print()

    current_bot = "[dim](already set)[/dim]" if "SLACK_BOT_TOKEN" in env else ""
    bot_token = menu.ask(f"Bot token (xoxb-...) {current_bot}", default="" if not current_bot else "<keep>")
    current_app = "[dim](already set)[/dim]" if "SLACK_APP_TOKEN" in env else ""
    app_token = menu.ask(f"App token (xapp-...) {current_app}", default="" if not current_app else "<keep>")

    if bot_token and bot_token != "<keep>":
        env["SLACK_BOT_TOKEN"] = bot_token
        secrets["SLACK_BOT_TOKEN"] = {"source": "env"}
    if app_token and app_token != "<keep>":
        env["SLACK_APP_TOKEN"] = app_token
        secrets["SLACK_APP_TOKEN"] = {"source": "env"}

    agent_ids = _agent_ids(config)
    if agent_ids:
        agent_options = [(a, a) for a in agent_ids]
        agent = menu.choose("Which agent handles Slack messages?", agent_options, default=_default_agent(config))
    else:
        agent = menu.ask("Agent ID", default="main")

    cfg = {
        "enabled": True,
        "botToken": "${SLACK_BOT_TOKEN}",
        "appToken": "${SLACK_APP_TOKEN}",
        "agent": agent,
    }
    return cfg, secrets, env


CHANNEL_CONFIGURATORS = {
    "telegram": ("Telegram", _configure_telegram),
    "slack":    ("Slack",    _configure_slack),
}


# ---------------------------------------------------------------------------
# Public step
# ---------------------------------------------------------------------------

def step_channels(config: dict, secrets: dict, env: dict) -> tuple[dict, dict, dict]:
    """Interactively configure channels section.

    Returns updated (config, secrets, env).
    """
    menu.section("Channels")

    if "channels" not in config:
        config["channels"] = {}

    channels = config["channels"]

    while True:
        if channels:
            menu.info("  Configured channels:")
            for cname in channels:
                menu.info(f"    [bold]{cname}[/bold]")
        else:
            menu.info("  No channels configured yet.")
        menu.console.print()

        options = [("add", "Add / reconfigure a channel")]
        if channels:
            options += [("remove", "Remove a channel")]
        options += [("done", "Done with channels  [dim](skip for TUI/HTTP only)[/dim]")]

        action = menu.choose("Action", options, default="done" if channels else "add")

        if action == "done":
            break

        elif action == "add":
            available = [(cid, label) for cid, (label, _) in CHANNEL_CONFIGURATORS.items()]
            cid = menu.choose("Which channel?", available)
            _, configurator = CHANNEL_CONFIGURATORS[cid]
            existing = channels.get(cid)
            cfg, secrets, env = configurator(existing, config, secrets, env)
            channels[cid] = cfg
            menu.success(f"Channel '{cid}' configured.")

        elif action == "remove":
            opts = [(c, c) for c in channels]
            cid = menu.choose("Remove which channel?", opts)
            if menu.confirm(f"Remove channel '{cid}'?", default=False):
                del channels[cid]

    config["channels"] = channels
    return config, secrets, env
