"""CLI entry point for pyclaw.

Usage:
    python -m pyclaw              # Run gateway + HTTP API
    python -m pyclaw --help      # Show help
    python -m pyclaw init        # Create default config
    python -m pyclaw validate     # Validate config
    python -m pyclaw run --tui   # Run with TUI
"""

import argparse
import asyncio
import sys
from pathlib import Path

from .config import load_config, create_default_config, find_config_file
from . import __version__


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="pyclaw",
        description="pyclaw - Python Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"pyclaw {__version__}",
    )

    parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to config file",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Create default configuration file",
    )
    init_parser.add_argument(
        "--path",
        "-p",
        type=str,
        default="~/.pyclaw/config.yaml",
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
        help="Update pyclaw to the latest release",
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
        help="Uninstall pyclaw",
    )
    uninstall_parser.add_argument(
        "--purge",
        action="store_true",
        help="Also remove ~/.pyclaw/ config and data without prompting",
    )

    # run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run the gateway server (with optional TUI)",
    )
    run_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind to (overrides config)",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind to (overrides config)",
    )
    run_parser.add_argument(
        "--tui",
        "-t",
        action="store_true",
        help="Launch the TUI instead of API server",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )

    return parser


def _register_skill_providers(mcp_server, config) -> None:
    """Mount each skill directory as a FastMCP SkillProvider (skill:// resources)."""
    try:
        from pathlib import Path
        from fastmcp.server.providers.skills import SkillProvider
        from pyclaw.skills.registry import get_skill_dirs

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


async def run_gateway(
    config_path: str = None, host: str = None, port: int = None, debug: bool = False
):
    """Run the gateway + HTTP API server + pyclaw MCP server."""
    from .config import ConfigLoader
    from .core.gateway import Gateway

    loader = ConfigLoader(config_path)
    config = loader.load()

    gw_host = host or config.gateway.host
    gw_port = port or config.gateway.port
    mcp_port = config.gateway.mcp_port

    print(f"pyclaw v{__version__}")
    print(f"Starting pyclaw MCP server on {gw_host}:{mcp_port}")
    print(f"Starting gateway + HTTP API on {gw_host}:{gw_port}")

    # MCP and API servers must be up BEFORE gateway.initialize() so that
    # FastAgent can connect to the MCP server during agent startup.
    gateway = Gateway(config_path)
    from .tools.server import mcp as pyclaw_mcp
    _register_skill_providers(pyclaw_mcp, config)
    await gateway.start_mcp_server(host=gw_host, port=mcp_port)
    await gateway.start_api_server(host=gw_host, port=gw_port)

    await gateway.initialize()

    # Start pulse runner
    if gateway.pulse_runner:
        await gateway.pulse_runner.start()
    gateway._is_running = True

    # Start Telegram polling
    if gateway._telegram_bot:
        gateway._telegram_polling_task = asyncio.create_task(gateway._telegram_poll())

    print(f"HTTP API docs: http://{gw_host}:{gw_port}/docs")
    print(f"MCP endpoint:  http://{gw_host}:{mcp_port}/mcp")
    print("Press Ctrl+C to stop...")

    try:
        # Keep running until interrupted; both servers run as background tasks
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gateway.stop()


async def run_gateway_with_tui(
    config_path: str = None, host: str = None, port: int = None, debug: bool = False
):
    """Run the gateway with TUI."""
    from .config import ConfigLoader
    from .core.gateway import Gateway

    loader = ConfigLoader(config_path)
    config = loader.load()

    gw_host = config.gateway.host
    gw_port = port or config.gateway.port
    mcp_port = config.gateway.mcp_port

    print(f"pyclaw v{__version__}")
    print("Starting gateway + TUI...")

    # MCP and API servers must be up BEFORE gateway.initialize() so that
    # FastAgent can connect to the MCP server during agent startup.
    gateway = Gateway(config_path)
    from .tools.server import mcp as pyclaw_mcp
    _register_skill_providers(pyclaw_mcp, config)
    await gateway.start_mcp_server(host=gw_host, port=mcp_port)
    await gateway.start_api_server(host=gw_host, port=gw_port)
    print(f"HTTP API: http://{gw_host}:{gw_port}/docs  |  MCP: http://{gw_host}:{mcp_port}/mcp")

    await gateway.initialize()

    # Start pulse runner without entering the blocking run-loop;
    # the TUI event-loop drives execution instead.
    if gateway.pulse_runner:
        await gateway.pulse_runner.start()
    gateway._is_running = True

    # Start Telegram polling (same as non-TUI mode)
    if gateway._telegram_bot:
        gateway._telegram_polling_task = asyncio.create_task(gateway._telegram_poll())

    # Run TUI with graceful shutdown handling
    try:
        from .tui.app import run_tui
        await run_tui(gateway)
    except KeyboardInterrupt:
        print("\nCtrl+C received, shutting down...")

    # Cleanup (with error handling)
    try:
        await gateway.stop()
    except Exception as e:
        print(f"Error during shutdown: {e}")


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
    config_path = args.config or "~/.pyclaw/config.yaml"

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


_REPO_SSH = "git+ssh://git@github.com/jondecker76/pyclaw.git"


def _latest_release_tag() -> str:
    """Return the latest semver tag from the remote repo via git ls-remote."""
    import re
    import subprocess
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", _REPO_SSH.replace("git+", ""), "v*"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    tags = re.findall(r"refs/tags/(v[\d]+\.[\d]+\.[\d]+)$", result.stdout, re.MULTILINE)
    if not tags:
        raise RuntimeError("No release tags found. Check your SSH access to GitHub.")
    return tags[0]


def cmd_update(args):
    """Handle update command."""
    import subprocess

    if args.beta:
        ref = "main"
        label = "latest from main (beta)"
    elif args.version:
        ref = args.version if args.version.startswith("v") else f"v{args.version}"
        label = f"version {ref}"
    else:
        print("Checking for latest release...")
        try:
            ref = _latest_release_tag()
        except Exception as e:
            print(f"✗ Could not determine latest release: {e}")
            sys.exit(1)
        label = f"latest stable release ({ref})"

    print(f"Installing pyclaw {label}...")
    url = f"{_REPO_SSH}@{ref}"
    try:
        subprocess.run(["uv", "tool", "install", "--reinstall", url], check=True)
        print(f"✓ pyclaw updated to {label}")
        print("  Your config and data in ~/.pyclaw/ are untouched.")
    except subprocess.CalledProcessError:
        print("✗ Update failed. Check the version/tag exists and your SSH access to GitHub.")
        sys.exit(1)


def cmd_uninstall(args):
    """Handle uninstall command."""
    import shutil
    import subprocess

    pyclaw_dir = Path("~/.pyclaw").expanduser()
    remove_data = False

    if args.purge:
        remove_data = True
    elif pyclaw_dir.exists():
        try:
            answer = input(f"Remove {pyclaw_dir} (config, sessions, memory)? [y/N] ").strip().lower()
            remove_data = answer == "y"
        except (EOFError, KeyboardInterrupt):
            print()
            remove_data = False

    try:
        subprocess.run(["uv", "tool", "uninstall", "pyclaw"], check=True)
    except subprocess.CalledProcessError:
        print("✗ Uninstall failed — is pyclaw installed via 'uv tool'?")
        sys.exit(1)

    if remove_data and pyclaw_dir.exists():
        shutil.rmtree(pyclaw_dir)
        print(f"✓ Removed {pyclaw_dir}")
    elif pyclaw_dir.exists():
        print(f"  Config and data kept at {pyclaw_dir}")

    print("✓ pyclaw uninstalled.")


def main():
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

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

    if args.command == "run":
        try:
            if args.tui:
                asyncio.run(run_gateway_with_tui(args.config, args.host, args.port, args.debug))
            else:
                asyncio.run(run_gateway(args.config, args.host, args.port, args.debug))
        except KeyboardInterrupt:
            print("\nShutting down...")
        return

    # Default: run gateway + HTTP API
    if args.command is None:
        try:
            asyncio.run(run_gateway(args.config, debug=args.debug))
        except KeyboardInterrupt:
            print("\nShutting down...")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
