"""Onboarding wizard orchestrator."""

import sys
from pathlib import Path

from . import menu
from .config_io import load_existing, write_all, has_config, config_path
from .steps import step_security, step_providers, step_agents, step_channels


# ---------------------------------------------------------------------------
# Requirements helpers
# ---------------------------------------------------------------------------

def _has_providers(config: dict) -> bool:
    return bool(config.get("providers"))


def _has_agents(config: dict) -> bool:
    return bool(config.get("agents"))


def _requirements_met(config: dict) -> bool:
    return _has_providers(config) and _has_agents(config)


# ---------------------------------------------------------------------------
# Unified setup menu (fresh install + reconfigure)
# ---------------------------------------------------------------------------

def _run_setup_menu(
    data_dir: Path,
    config: dict,
    secrets: dict,
    env: dict,
    fresh: bool = False,
) -> tuple[dict, dict, dict] | None:
    """Show the main setup checklist menu.

    Returns (config, secrets, env) when the user saves, or None if they quit.
    """
    if fresh:
        step_security()

    while True:
        has_providers = _has_providers(config)
        has_agents = _has_agents(config)
        can_save = has_providers and has_agents

        menu.section("Setup Menu")

        if not can_save:
            menu.info("[dim]Items marked [bold red]*[/bold red][dim] are required.[/dim]")
            menu.console.print()

        # Build options with status indicators
        options: list[tuple[str, str]] = [
            ("providers", menu.required_label(
                f"Providers"
                + (f"  [dim]({', '.join(config['providers'].keys())})[/dim]" if has_providers else ""),
                has_providers,
            )),
            ("agents", menu.required_label(
                f"Agents"
                + (f"  [dim]({', '.join(config['agents'].keys())})[/dim]" if has_agents else ""),
                has_agents,
            )),
            ("channels",
                "   Channels"
                + (f"  [dim]({', '.join(config.get('channels', {}).keys())})[/dim]"
                   if config.get("channels") else "  [dim](optional)[/dim]"),
            ),
        ]

        if can_save:
            options.append(("save", "[green]Save & finish[/green]"))

        action = menu.choose("Choose an option", options, default="save" if can_save else "providers")

        # q → quit
        if action is None:
            menu.quit_wizard()
            continue  # user said no to "really quit?" — loop back

        if action == "save":
            break

        elif action == "providers":
            config, secrets, env = step_providers(config, secrets, env)

        elif action == "agents":
            if not has_providers:
                menu.warn("Configure at least one provider before adding agents.")
                continue
            config, secrets, env = step_agents(config, secrets, env)

        elif action == "channels":
            config, secrets, env = step_channels(config, secrets, env)

    # Ensure required top-level keys
    if "gateway" not in config:
        config["gateway"] = {"host": "0.0.0.0", "port": 8080, "log_level": "info"}
    if "version" not in config:
        config["version"] = "1.0"

    return config, secrets, env


# ---------------------------------------------------------------------------
# Summary + optional launch
# ---------------------------------------------------------------------------

def _print_summary(data_dir: Path, config: dict) -> None:
    menu.section("Setup Complete", style="green")
    menu.success(f"Config:   {data_dir}/config/pyclopse.yaml")
    menu.success(f"Secrets:  {data_dir}/secrets/secrets.yaml")
    menu.success(f"Env:      {data_dir}/.env  [dim](chmod 600)[/dim]")
    menu.console.print()
    for aid, acfg in config.get("agents", {}).items():
        menu.info(f"  Agent [bold]{acfg.get('name', aid)}[/bold] ([dim]{aid}[/dim])  —  {acfg.get('model', '?')}")
    menu.console.print()
    default_dir = Path("~/.pyclopse").expanduser()
    menu.info("[dim]  To start:[/dim]")
    if data_dir.resolve() == default_dir.resolve():
        menu.info("    pyclopse")
    else:
        menu.info(f"    pyclopse --config {data_dir}/config/pyclopse.yaml")
    menu.console.print()


def _maybe_launch(data_dir: Path) -> None:
    if menu.confirm("Start pyclopse now?", default=True):
        import asyncio
        from pyclopse.__main__ import run_gateway_with_tui
        asyncio.run(run_gateway_with_tui(config_path=str(config_path(data_dir))))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_onboard(data_dir: Path, section: str | None = None) -> None:
    """Run the onboarding wizard.

    Args:
        data_dir:  Root data directory (e.g. ~/.pyclopse or ~/.pyclopse_test).
        section:   If set, jump directly to 'providers', 'agents', or 'channels'.
    """
    menu.header(data_dir)

    existing = has_config(data_dir)
    config, secrets, env = load_existing(data_dir)

    if section:
        # Jump straight to a specific section, then drop into menu
        step_security()
        if section == "providers":
            config, secrets, env = step_providers(config, secrets, env)
        elif section == "agents":
            config, secrets, env = step_agents(config, secrets, env)
        elif section == "channels":
            config, secrets, env = step_channels(config, secrets, env)
        else:
            menu.warn(f"Unknown section '{section}'. Choose: providers, agents, channels.")
            sys.exit(1)
        # After the targeted section, drop into the full menu so they can continue
        result = _run_setup_menu(data_dir, config, secrets, env, fresh=False)
    else:
        result = _run_setup_menu(data_dir, config, secrets, env, fresh=not existing)

    if result is None:
        return
    config, secrets, env = result

    write_all(data_dir, config, secrets, env)
    _print_summary(data_dir, config)
    _maybe_launch(data_dir)
