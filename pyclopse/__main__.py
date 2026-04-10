"""CLI entry point for pyclopse.

Usage:
    pyclopse                        # Run gateway + embedded TUI dashboard
    pyclopse --headless             # Run gateway as a service (no TUI)
    pyclopse tui                    # Connect TUI to a running gateway
    pyclopse tui --url host:8080    # Connect to a remote gateway
    pyclopse --config ~/my.yaml    # Use a specific config file
    pyclopse --host 0.0.0.0 --port 9000
    pyclopse onboard                # First-time setup wizard
    pyclopse validate               # Validate config
    pyclopse update                 # Update to latest release
"""

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import load_config, create_default_config, find_config_file
from . import __version__


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="pyclopse",
        description="pyclopse - Python Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"pyclopse {__version__}",
    )

    parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to config file",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without TUI (stdout only)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind to (overrides config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind to (overrides config)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # onboard command
    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Interactive setup wizard (first-time or reconfigure)",
    )
    onboard_parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Data directory to use (default: ~/.pyclopse)",
    )
    onboard_parser.add_argument(
        "--providers",
        action="store_true",
        help="Jump directly to provider configuration",
    )
    onboard_parser.add_argument(
        "--agents",
        action="store_true",
        help="Jump directly to agent configuration",
    )
    onboard_parser.add_argument(
        "--channels",
        action="store_true",
        help="Jump directly to channel configuration",
    )

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Create default configuration file",
    )
    init_parser.add_argument(
        "--path",
        "-p",
        type=str,
        default="~/.pyclopse/config.yaml",
        help="Path where to create config",
    )
    init_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing config",
    )

    # validate command
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate configuration file",
    )
    validate_parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to config file",
    )

    # update command
    update_parser = subparsers.add_parser(
        "update",
        help="Update pyclopse to the latest release",
    )
    update_parser.add_argument(
        "--beta",
        action="store_true",
        help="Install the latest commit from main (unstable)",
    )
    update_parser.add_argument(
        "--version",
        type=str,
        default=None,
        metavar="VERSION",
        help="Install a specific version, e.g. 0.2.1",
    )

    # uninstall command
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Uninstall pyclopse",
    )
    uninstall_parser.add_argument(
        "--purge",
        action="store_true",
        help="Also remove ~/.pyclopse/ config and data without prompting",
    )

    # import-openclaw command
    import_parser = subparsers.add_parser(
        "import-openclaw",
        help="Import OpenClaw session history into pyclopse",
    )
    import_parser.add_argument(
        "--agent",
        type=str,
        default=None,
        metavar="NAME",
        help="Import sessions for a specific agent name",
    )
    import_parser.add_argument(
        "--all",
        action="store_true",
        help="Import sessions for all discovered agents",
    )
    import_parser.add_argument(
        "--openclaw-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Path to OpenClaw data directory (default: ~/.openclaw)",
    )
    import_parser.add_argument(
        "--pyclopse-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Path to pyclopse data directory (default: ~/.pyclopse)",
    )

    # secret command
    secret_parser = subparsers.add_parser(
        "secret",
        help="Manage secrets (retrieve values from configured providers)",
    )
    secret_sub = secret_parser.add_subparsers(dest="secret_command", help="Secret commands")

    secret_get_parser = secret_sub.add_parser("get", help="Retrieve a secret value by name")
    secret_get_parser.add_argument("name", help="Secret name as registered in secrets config (e.g. MINIMAX_API_KEY)")

    # tui command — connect dashboard to a running gateway
    tui_parser = subparsers.add_parser(
        "tui",
        help="Launch TUI dashboard (connects to a running gateway)",
    )
    tui_parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:8080",
        help="Gateway URL to connect to (default: http://localhost:8080)",
    )

    # service command — manage pyclopse as a system service
    service_parser = subparsers.add_parser(
        "service",
        help="Manage pyclopse as a background service (launchd/systemd)",
    )
    service_sub = service_parser.add_subparsers(dest="service_command", help="Service commands")
    service_sub.add_parser("install", help="Install and enable the service")
    service_sub.add_parser("uninstall", help="Disable and remove the service")
    service_sub.add_parser("start", help="Start the service")
    service_sub.add_parser("stop", help="Stop the service")
    service_sub.add_parser("restart", help="Restart the service")
    service_sub.add_parser("status", help="Check service status")
    _logs_p = service_sub.add_parser("logs", help="Tail service logs")
    _logs_p.add_argument("-n", "--lines", type=int, default=50, help="Number of lines (default: 50)")

    return parser


class _ExcludeAgentDetailFilter(logging.Filter):
    """Block pyclopse.agent.* INFO/DEBUG records from the main pyclopse.log.

    Per-agent conversation turns and tool calls are noisy and belong in the
    per-agent log, not in the broad gateway log.  WARNING+ still passes through
    so errors and warnings surface in both places.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if record.name.startswith("pyclopse.agent.") and record.levelno < logging.WARNING:
            return False
        return True


def setup_logging(config, debug: bool = False) -> None:
    """Configure root logger: console + daily-rotating file under ~/.pyclopse/logs/."""
    level_name = "DEBUG" if debug else config.gateway.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)-30s %(levelname)-8s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Daily rotating file handler → ~/.pyclopse/logs/pyclopse.log
    logs_dir = Path("~/.pyclopse/logs").expanduser()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "pyclopse.log"

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        backupCount=config.gateway.log_retention_days,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    # Suppress per-agent detail from the main gateway log
    file_handler.addFilter(_ExcludeAgentDetailFilter())

    # Rename rotated files from "pyclopse.log.YYYY-MM-DD" → "pyclopse-YYYY-MM-DD.log"
    def _namer(default_name: str) -> str:
        base, _, suffix = default_name.rpartition(".")
        if suffix and len(suffix) == 10:  # YYYY-MM-DD
            return str(logs_dir / f"pyclopse-{suffix}.log")
        return default_name

    file_handler.namer = _namer
    root.addHandler(file_handler)


def setup_agent_logging(agent_id: str, logs_dir: Path, retention_days: int) -> None:
    """Set up a daily-rotating per-agent log under ~/.pyclopse/agents/{agent_id}/logs/.

    All records emitted to logger ``pyclopse.agent.{agent_id}`` (and its children)
    are written to ``agent.log`` in addition to normal propagation.  The file is
    created on first write; the parent directory is created here.
    """
    agent_log_dir = logs_dir / agent_id / "logs"
    agent_log_dir.mkdir(parents=True, exist_ok=True)

    agent_logger = logging.getLogger(f"pyclopse.agent.{agent_id}")
    # Avoid duplicate handlers if called more than once (e.g. gateway restart)
    for h in agent_logger.handlers:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    fh = logging.handlers.TimedRotatingFileHandler(
        filename=agent_log_dir / "agent.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
    )

    def _namer(default_name: str) -> str:
        base, _, suffix = default_name.rpartition(".")
        if suffix and len(suffix) == 10:  # YYYY-MM-DD
            return str(agent_log_dir / f"agent-{suffix}.log")
        return default_name

    fh.namer = _namer
    fh.setFormatter(fmt)
    agent_logger.addHandler(fh)
    # Do NOT set propagate=False so records also flow to root (console + pyclopse.log at WARNING+)
    agent_logger.setLevel(logging.DEBUG)



def _register_skill_providers(mcp_server, config) -> None:
    """Mount each skill directory as a FastMCP SkillProvider (skill:// resources)."""
    try:
        from pathlib import Path
        from fastmcp.server.providers.skills import SkillProvider
        from pyclopse.skills.registry import get_skill_dirs

        extra_dirs = list(config.gateway.skills_dirs) if config.gateway.skills_dirs else []
        skill_dirs = get_skill_dirs(extra_dirs=extra_dirs)
        registered = 0
        for skills_root in skill_dirs:
            for entry in sorted(skills_root.iterdir()):
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    try:
                        mcp_server.add_provider(SkillProvider(entry))
                        registered += 1
                    except Exception as e:
                        print(f"  [warn] Could not register skill {entry.name}: {e}")
        if registered:
            print(f"Registered {registered} skill(s) as MCP resources")
    except Exception as e:
        print(f"  [warn] Skill provider registration skipped: {e}")


def _force_exit() -> None:
    """Force the process to exit after gateway.stop() completes.

    FastAgent spawns asyncio tasks via create_task() that are not tied to any
    context we control.  These tasks keep the event loop alive indefinitely
    after shutdown.  os._exit() bypasses all remaining asyncio machinery and
    exits immediately — logs are flushed by the logging shutdown hook registered
    via atexit, so nothing is lost.
    """
    import logging as _logging
    import os as _os
    _logging.shutdown()
    _os._exit(0)


def _is_service_installed() -> bool:
    """Check if pyclopse is installed as a system service."""
    import sys as _s
    if _s.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "com.pyclopse.gateway.plist").exists()
    elif _s.platform.startswith("linux"):
        return (Path.home() / ".config" / "systemd" / "user" / "pyclopse.service").exists()
    return False


def _print_service_hint() -> None:
    """Print a hint about service install if not already installed."""
    if not _is_service_installed():
        print()
        print("  TIP: Run pyclopse as a background service that starts on login:")
        print("       pyclopse service install")
        print("       Then connect the dashboard anytime with: pyclopse tui")
        print()


async def run_gateway(
    config_path: str = None, host: str = None, port: int = None, debug: bool = False
):
    """Run the gateway + HTTP API server + pyclopse MCP server."""
    from .core.singleton import acquire_gateway_lock, GatewayAlreadyRunning
    try:
        acquire_gateway_lock()
    except GatewayAlreadyRunning as e:
        print(f"Error: {e}")
        sys.exit(1)

    from .config import ConfigLoader
    from .core.gateway import Gateway

    loader = ConfigLoader(config_path)
    config = loader.load()
    setup_logging(config, debug=debug)

    gw_host = host or config.gateway.host
    gw_port = port or config.gateway.port
    mcp_port = config.gateway.mcp_port

    print(f"pyclopse v{__version__}")
    print(f"Starting pyclopse MCP server on {gw_host}:{mcp_port}")
    print(f"Starting gateway + HTTP API on {gw_host}:{gw_port}")

    # MCP and API servers must be up BEFORE gateway.initialize() so that
    # FastAgent can connect to the MCP server during agent startup.
    gateway = Gateway(config_path)
    from .tools.server import mcp as pyclopse_mcp
    _register_skill_providers(pyclopse_mcp, config)
    await gateway.start_mcp_server(host=gw_host, port=mcp_port)
    await gateway.start_api_server(host=gw_host, port=gw_port)

    await gateway.initialize()

    # Set up per-agent log files now that agents are initialized
    _agents_logs_dir = Path("~/.pyclopse/agents").expanduser()
    _retention = config.gateway.log_retention_days
    if gateway._agent_manager:
        for _aid in gateway._agent_manager.agents:
            setup_agent_logging(_aid, _agents_logs_dir, _retention)

    gateway._is_running = True

    # Telegram polling is now started by TelegramPlugin.start() during gateway.initialize()
    if "telegram" in gateway._channels:
        print("Telegram channel plugin active")

    print(f"HTTP API docs: http://{gw_host}:{gw_port}/docs")
    print(f"MCP endpoint:  http://{gw_host}:{mcp_port}/mcp")
    print("Press Ctrl+C to stop...")

    try:
        # Keep running until interrupted; all servers run as background tasks
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gateway.stop()
        _force_exit()


async def run_gateway_with_tui(
    config_path: str = None, host: str = None, port: int = None, debug: bool = False
):
    """Run the gateway with the dashboard TUI."""
    from .core.singleton import acquire_gateway_lock, GatewayAlreadyRunning
    try:
        acquire_gateway_lock()
    except GatewayAlreadyRunning as e:
        print(f"Error: {e}")
        sys.exit(1)

    from .config import ConfigLoader
    from .core.gateway import Gateway

    loader = ConfigLoader(config_path)
    config = loader.load()
    setup_logging(config, debug=debug)

    gw_host = config.gateway.host
    gw_port = port or config.gateway.port
    mcp_port = config.gateway.mcp_port

    print(f"pyclopse v{__version__}")
    _print_service_hint()
    print("Starting gateway + dashboard...")

    # MCP and API servers must be up BEFORE gateway.initialize() so that
    # FastAgent can connect to the MCP server during agent startup.
    gateway = Gateway(config_path)
    from .tools.server import mcp as pyclopse_mcp
    _register_skill_providers(pyclopse_mcp, config)
    await gateway.start_mcp_server(host=gw_host, port=mcp_port)
    await gateway.start_api_server(host=gw_host, port=gw_port)
    print(f"HTTP API: http://{gw_host}:{gw_port}/docs  |  MCP: http://{gw_host}:{mcp_port}/mcp")

    await gateway.initialize()

    # Set up per-agent log files now that agents are initialized
    _agents_logs_dir = Path("~/.pyclopse/agents").expanduser()
    _retention = config.gateway.log_retention_days
    if gateway._agent_manager:
        for _aid in gateway._agent_manager.agents:
            setup_agent_logging(_aid, _agents_logs_dir, _retention)

    gateway._is_running = True

    # Telegram polling is now started by TelegramPlugin.start() during gateway.initialize()
    if "telegram" in gateway._channels:
        print("Telegram channel plugin active")

    # Run the dashboard TUI (replaces the old multi-screen TUI)
    try:
        from .tui.dashboard import run_dashboard
        await run_dashboard(gateway)
    except KeyboardInterrupt:
        print("\nCtrl+C received, shutting down...")

    # Cleanup (with error handling)
    try:
        await gateway.stop()
    except Exception as e:
        print(f"Error during shutdown: {e}")
    _force_exit()


async def run_tui_remote(url: str = "http://localhost:8080"):
    """Launch the TUI dashboard connected to a remote gateway."""
    from .tui.remote_client import RemoteGatewayClient
    from .tui.dashboard import run_dashboard

    client = RemoteGatewayClient(url)
    try:
        await client.connect()
    except ConnectionError as e:
        print(f"Error: {e}")
        print(f"Is the gateway running? Start it with: pyclopse --headless")
        return
    print(f"Connected to gateway at {url}")
    try:
        await run_dashboard(client=client)
    finally:
        await client.close()


def cmd_service(args):
    """Handle service command."""
    from .service.manager import get_manager

    subcmd = getattr(args, "service_command", None)
    if not subcmd:
        print("Usage: pyclopse service {install|uninstall|start|stop|restart|status|logs}")
        return

    try:
        mgr = get_manager()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if subcmd == "install":
        print(mgr.install())
    elif subcmd == "uninstall":
        print(mgr.uninstall())
    elif subcmd == "start":
        print(mgr.start())
    elif subcmd == "stop":
        print(mgr.stop())
    elif subcmd == "restart":
        print(mgr.restart())
    elif subcmd == "status":
        print(mgr.status())
    elif subcmd == "logs":
        lines = getattr(args, "lines", 50)
        print(mgr.logs(lines))


def cmd_onboard(args):
    """Handle onboard command."""
    data_dir_str = args.data_dir or "~/.pyclopse"
    data_dir = Path(data_dir_str).expanduser()

    # Determine which section to jump to (mutually exclusive flags)
    section = None
    if getattr(args, "providers", False):
        section = "providers"
    elif getattr(args, "agents", False):
        section = "agents"
    elif getattr(args, "channels", False):
        section = "channels"

    from .onboard import run_onboard
    run_onboard(data_dir, section=section)


def _check_needs_onboard(config_path_override: str | None = None) -> Path | None:
    """Return the default data dir if no config exists, else None."""
    from .config.loader import find_config_file
    if config_path_override:
        return None  # explicit path — user knows what they're doing
    if find_config_file() is None:
        return Path("~/.pyclopse").expanduser()
    return None


def cmd_init(args):
    """Handle init command."""
    path = Path(args.path).expanduser()

    if path.exists() and not args.force:
        print(f"Config already exists at {path}. Use --force to overwrite.")
        sys.exit(1)

    create_default_config(path)
    print(f"Created default config at {path}")


def cmd_validate(args):
    """Handle validate command."""
    config_path = args.config or "~/.pyclopse/config.yaml"

    try:
        config = load_config(config_path)
        print("✓ Configuration is valid")
        print(f"  Version: {config.version}")
        print(f"  Gateway: {config.gateway.host}:{config.gateway.port}")
        print(f"  Security mode: {config.security.exec_approvals.mode}")
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Configuration error: {e}")
        sys.exit(1)


def cmd_secret(args):
    """Handle secret command."""
    if args.secret_command != "get":
        print("Usage: pyclopse secret get <name>")
        sys.exit(1)

    from pyclopse.secrets.manager import SecretsManager, ResolutionError
    from pyclopse.config.loader import load_secrets_registry, find_config_file, expand_path

    config_path = getattr(args, "config", None)
    cfg_path = expand_path(config_path) if config_path else find_config_file()
    manager = SecretsManager(load_secrets_registry(cfg_path))
    try:
        print(manager.resolve_name(args.name))
    except ResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


_REPO_HTTPS = "git+https://github.com/jondecker76/pyclopse.git"


def cmd_update(args):
    """Handle update command."""
    import subprocess

    if args.beta:
        ref = "main"
        label = "latest from main (beta)"
        url = f"{_REPO_HTTPS}@{ref}"
        print(f"Installing pyclopse {label}...")
        try:
            subprocess.run(["uv", "tool", "install", "--reinstall", url], check=True)
            print(f"✓ pyclopse updated to {label}")
            print("  Your config and data in ~/.pyclopse/ are untouched.")
        except subprocess.CalledProcessError:
            print("✗ Update failed.")
            sys.exit(1)
    elif args.version:
        version = args.version if args.version.startswith("v") else f"v{args.version}"
        label = f"version {version}"
        url = f"{_REPO_HTTPS}@{version}"
        print(f"Installing pyclopse {label}...")
        try:
            subprocess.run(["uv", "tool", "install", "--reinstall", url], check=True)
            print(f"✓ pyclopse updated to {label}")
            print("  Your config and data in ~/.pyclopse/ are untouched.")
        except subprocess.CalledProcessError:
            print(f"✗ Update failed. Check that version {version} exists.")
            sys.exit(1)
    else:
        print("Upgrading pyclopse to the latest release...")
        try:
            subprocess.run(["uv", "tool", "upgrade", "pyclopse"], check=True)
            print("✓ pyclopse upgraded.")
            print("  Your config and data in ~/.pyclopse/ are untouched.")
        except subprocess.CalledProcessError:
            print("✗ Upgrade failed. Is pyclopse installed via 'uv tool'?")
            sys.exit(1)


def cmd_uninstall(args):
    """Handle uninstall command."""
    import shutil
    import subprocess

    pyclopse_dir = Path("~/.pyclopse").expanduser()
    remove_data = False

    if args.purge:
        remove_data = True
    elif pyclopse_dir.exists():
        try:
            answer = input(f"Remove {pyclopse_dir} (config, sessions, memory)? [y/N] ").strip().lower()
            remove_data = answer == "y"
        except (EOFError, KeyboardInterrupt):
            print()
            remove_data = False

    try:
        subprocess.run(["uv", "tool", "uninstall", "pyclopse"], check=True)
    except subprocess.CalledProcessError:
        print("✗ Uninstall failed — is pyclopse installed via 'uv tool'?")
        sys.exit(1)

    if remove_data and pyclopse_dir.exists():
        shutil.rmtree(pyclopse_dir)
        print(f"✓ Removed {pyclopse_dir}")
    elif pyclopse_dir.exists():
        print(f"  Config and data kept at {pyclopse_dir}")

    print("✓ pyclopse uninstalled.")


def main():
    """Main entry point."""
    import sys as _sys
    # `pyclopse run [flags]` → strip "run" and treat flags as top-level (backward compat)
    if len(_sys.argv) > 1 and _sys.argv[1] == "run":
        _sys.argv.pop(1)

    parser = create_parser()
    args = parser.parse_args()

    if args.command == "onboard":
        cmd_onboard(args)
        return

    if args.command == "service":
        cmd_service(args)
        return

    if args.command == "init":
        cmd_init(args)
        return

    if args.command == "validate":
        cmd_validate(args)
        return

    if args.command == "update":
        cmd_update(args)
        return

    if args.command == "uninstall":
        cmd_uninstall(args)
        return

    if args.command == "secret":
        cmd_secret(args)
        return

    if args.command == "import-openclaw":
        from .tools.openclaw_import import cmd_import_openclaw
        cmd_import_openclaw(args)
        return

    if args.command == "tui":
        try:
            asyncio.run(run_tui_remote(args.url))
        except KeyboardInterrupt:
            print("\nTUI closed.")
        return

    # Default: run gateway (bare `pyclopse`)
    if args.command is None:
        needs_onboard = _check_needs_onboard(args.config)
        if needs_onboard:
            print("No configuration found.")
            from .onboard.menu import confirm
            if confirm("Run setup wizard now?", default=True):
                from .onboard import run_onboard
                run_onboard(needs_onboard)
            else:
                print("Run 'pyclopse onboard' when you're ready to set up.")
            return
        try:
            if args.headless:
                asyncio.run(run_gateway(args.config, args.host, args.port, args.debug))
            else:
                asyncio.run(run_gateway_with_tui(args.config, args.host, args.port, args.debug))
        except KeyboardInterrupt:
            print("\nShutting down...")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
