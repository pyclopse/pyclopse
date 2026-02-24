"""CLI entry point for pyclaw.

Usage:
    python -m pyclaw              # Run gateway
    python -m pyclaw --help      # Show help
    python -m pyclaw init        # Create default config
    python -m pyclaw validate     # Validate config
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
        "--version", "-v",
        action="version",
        version=f"pyclaw {__version__}",
    )
    
    parser.add_argument(
        "--config", "-c",
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
        "--path", "-p",
        type=str,
        default="~/.pyclaw/config.yaml",
        help="Path where to create config",
    )
    init_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing config",
    )
    
    # validate command
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate configuration file",
    )
    validate_parser.add_argument(
        "--config", "-c",
        type=str,
        help="Path to config file",
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
        "--tui", "-t",
        action="store_true",
        help="Launch the TUI instead of API server",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )
    
    return parser


async def run_gateway(config_path: str = None, host: str = None, port: int = None, debug: bool = False):
    """Run the gateway server."""
    from .config import ConfigLoader
    from .core.gateway import Gateway
    
    loader = ConfigLoader(config_path)
    config = loader.load()
    
    # Override host/port if provided
    if host:
        config.gateway.host = host
    if port:
        config.gateway.port = port
    
    print(f"pyclaw v{__version__}")
    print(f"Starting gateway on {config.gateway.host}:{config.gateway.port}")
    print(f"Debug: {debug}")
    
    # Create and start gateway
    gateway = Gateway(config_path)
    await gateway.start()
    
    print("\nGateway is running!")
    print("Press Ctrl+C to stop...")
    
    # Keep running until interrupted
    try:
        while gateway._is_running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        await gateway.stop()


async def run_gateway_with_tui(config_path: str = None, host: str = None, port: int = None, debug: bool = False):
    """Run the gateway with TUI."""
    from .config import ConfigLoader
    from .core.gateway import Gateway
    
    loader = ConfigLoader(config_path)
    config = loader.load()
    
    # Override host/port if provided
    if host:
        config.gateway.host = host
    if port:
        config.gateway.port = port
    
    print(f"pyclaw v{__version__}")
    print(f"Starting gateway + TUI...")
    
    # Create gateway (don't start API server, just initialize core)
    gateway = Gateway(config_path)
    await gateway.initialize()
    
    # Run TUI with graceful shutdown handling
    try:
        from .tui.app import run_tui
        await run_tui(gateway)
    except KeyboardInterrupt:
        print("\nCtrl+C received, shutting down...")
    
    # Cleanup
    await gateway.stop()


def cmd_init(args):
    """Handle init command."""
    path = Path(args.path).expanduser()
    
    if path.exists() and not args.force:
        print(f"Config already exists at {path}. Use --force to overwrite.")
        sys.exit(1)
    
    # Create default config
    create_default_config(path)
    print(f"Created default config at {path}")


def cmd_validate(args):
    """Handle validate command."""
    config_path = args.config or "~/.pyclaw/config.yaml"
    
    try:
        config = load_config(config_path)
        print(f"✓ Configuration is valid")
        print(f"  Version: {config.version}")
        print(f"  Gateway: {config.gateway.host}:{config.gateway.port}")
        print(f"  Security mode: {config.security.exec_approvals.mode}")
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Configuration error: {e}")
        sys.exit(1)


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
    
    if args.command == "run":
        if args.tui:
            asyncio.run(run_gateway_with_tui(args.config, args.host, args.port, args.debug))
        else:
            asyncio.run(run_gateway(args.config, args.host, args.port, args.debug))
        return
    
    # Default: run gateway
    if args.command is None:
        asyncio.run(run_gateway(args.config, debug=args.debug))
        return
    
    parser.print_help()


if __name__ == "__main__":
    main()
