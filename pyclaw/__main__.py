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
    
    return parser


async def run_gateway(config_path: str = None, debug: bool = False):
    """Run the gateway server."""
    from .config import ConfigLoader
    
    loader = ConfigLoader(config_path)
    config = loader.load()
    
    print(f"pyclaw v{__version__}")
    print(f"Loaded config: {config.gateway.host}:{config.gateway.port}")
    print(f"Debug: {debug}")
    
    # TODO: Implement actual gateway run
    # For now, just print config summary
    print("\nConfiguration summary:")
    print(f"  Gateway: {config.gateway.host}:{config.gateway.port}")
    print(f"  Security: {config.security.exec_approvals.mode}")
    print(f"  Memory backend: {config.memory.backend}")
    print(f"  Providers: {config.providers.model_dump(exclude_none=True)}")
    print(f"  Channels: {config.channels.model_dump(exclude_none=True)}")
    print(f"  Jobs: {config.jobs.enabled}")
    print(f"  TUI: {config.tui.enabled}")
    
    print("\nGateway startup not implemented yet.")


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
    
    # Default: run gateway
    if args.command is None:
        asyncio.run(run_gateway(args.config, args.debug))
        return
    
    parser.print_help()


if __name__ == "__main__":
    main()
