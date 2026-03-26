"""Security notice step."""

import sys
from .. import menu


def step_security() -> None:
    """Display security notice and require acknowledgment.

    Exits the process if the user declines.
    """
    menu.section("Security Notice", style="yellow")
    menu.info("[yellow]pyclopse agents can:[/yellow]")
    menu.info("  • Read and write files on your filesystem")
    menu.info("  • Execute shell commands (if exec tools are used)")
    menu.info("  • Send messages to connected channels on your behalf")
    menu.console.print()
    menu.info("[dim]Best practices:[/dim]")
    menu.info("  • Only allow users you trust in channel configs")
    menu.info("  • Keep API keys in secrets / .env, not in config.yaml")
    menu.info("  • Review exec_approvals before enabling shell tools")
    menu.console.print()
    if not menu.confirm("I understand — continue with setup?", default=True):
        menu.warn("Setup cancelled.")
        sys.exit(0)
