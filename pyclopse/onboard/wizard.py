"""Onboarding wizard orchestrator."""

import sys
from pathlib import Path

from .config_io import load_existing, write_all, has_config, config_path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_onboard(data_dir: Path, section: str | None = None) -> None:
    """Run the TUI onboarding wizard.

    Args:
        data_dir:  Root data directory (e.g. ~/.pyclopse or ~/.pyclopse_test).
        section:   If set, jump directly to 'providers', 'agents', or 'channels'.
                   (The TUI always starts from the main menu, so this is a hint
                    only — the user can navigate freely from there.)
    """
    existing = has_config(data_dir)
    config, secrets, env = load_existing(data_dir)

    if section and section not in ("providers", "agents", "channels"):
        from . import menu
        menu.warn(f"Unknown section '{section}'. Choose: providers, agents, channels.")
        sys.exit(1)

    from .tui import run_tui_wizard
    config, secrets, env, launch_mode = run_tui_wizard(
        data_dir=data_dir,
        config=config,
        secrets=secrets,
        env=env,
        fresh=not existing,
    )

    # Only write if the user completed the wizard (reached SummaryScreen).
    if config.get("providers") and config.get("agents"):
        write_all(data_dir, config, secrets, env)

    cfg_path = str(config_path(data_dir))

    if launch_mode == "service":
        _install_and_launch_service()
    elif launch_mode == "embedded":
        import asyncio
        from pyclopse.__main__ import run_gateway_with_tui
        asyncio.run(run_gateway_with_tui(config_path=cfg_path))


def _install_and_launch_service() -> None:
    """Install the service, start it, then launch the TUI to connect."""
    import asyncio

    try:
        from pyclopse.service.manager import get_manager
        mgr = get_manager()
        print(mgr.install())
        print(mgr.start())
        print()
        print("Service installed and running.")
        print("Connecting TUI dashboard...")
        print()

        from pyclopse.__main__ import run_tui_remote
        asyncio.run(run_tui_remote())

    except RuntimeError as e:
        print(f"Error: {e}")
        print("Service management is not available on this platform.")
        print("You can start pyclopse manually with: pyclopse --headless")
        sys.exit(1)
    except Exception as e:
        print(f"Service install failed: {e}")
        print("You can start pyclopse manually with: pyclopse --headless")
        sys.exit(1)
