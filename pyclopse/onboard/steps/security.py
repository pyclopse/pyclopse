"""Security notice step."""

import sys
from rich.panel import Panel
from rich.text import Text
from rich import box as rbox
from .. import menu


def step_security() -> None:
    """Display security notice and require acknowledgment.

    Exits the process if the user declines.
    """
    menu.section("Security Notice", style="yellow")

    body = Text()
    body.append("\n")
    body.append("  ▲  CAPABILITIES\n", style="bold yellow")
    body.append("\n")
    body.append("     ●  Read and write files on your filesystem\n", style="yellow")
    body.append("     ●  Execute shell commands (if exec tools are enabled)\n", style="yellow")
    body.append("     ●  Send messages to connected channels on your behalf\n", style="yellow")
    body.append("\n")
    body.append("  ─  BEST PRACTICES\n", style="bold yellow")
    body.append("\n")
    body.append("     ●  Only allow users you trust in channel configs\n", style="dim yellow")
    body.append("     ●  Keep API keys in secrets / .env, not in config.yaml\n", style="dim yellow")
    body.append("     ●  Review exec_approvals before enabling shell tools\n", style="dim yellow")
    body.append("\n")

    menu.console.print(Panel(
        body,
        border_style="yellow",
        box=rbox.DOUBLE,
        padding=(0, 1),
        title="[bold yellow]! SECURITY ADVISORY ![/bold yellow]",
    ))
    menu.console.print()

    if not menu.confirm("I UNDERSTAND — CONTINUE WITH SETUP?", default=True):
        menu.warn("SETUP CANCELLED.")
        sys.exit(0)
