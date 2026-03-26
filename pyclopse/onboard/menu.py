"""Shared UI utilities for the onboarding wizard."""

import sys
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich.text import Text
from rich.table import Table
from rich import print as rprint
from typing import Optional

console = Console()


def header(data_dir) -> None:
    console.print()
    console.print(Panel.fit(
        Text.assemble(("pyclopse", "bold cyan"), (" — setup", "dim")),
        border_style="cyan",
    ))
    console.print(f"  Data directory: [bold]{data_dir}[/bold]")
    console.print()


def section(title: str, style: str = "cyan") -> None:
    console.print()
    console.print(Rule(title, style=style))
    console.print()


def choose(
    prompt: str,
    options: list[tuple[str, str]],
    default: str = "1",
    allow_quit: bool = True,
) -> str | None:
    """Present a numbered menu and return the selected key.

    Returns None if the user types 'q' (quit), when allow_quit is True.

    Args:
        options:     list of (key, label) tuples.
        default:     the key that is pre-selected.
        allow_quit:  if True, 'q' is accepted and returns None.
    """
    default_idx = default
    for i, (key, label) in enumerate(options, 1):
        is_default = (key == default)
        marker = "  [dim]← default[/dim]" if is_default else ""
        if is_default:
            default_idx = str(i)
        console.print(f"  [bold]{i}[/bold]  {label}{marker}")

    if allow_quit:
        console.print(f"  [bold dim]q[/bold dim]  [dim]Quit[/dim]")

    console.print()
    choices = [str(i) for i in range(1, len(options) + 1)]
    if allow_quit:
        choices.append("q")

    choice = Prompt.ask(prompt, choices=choices, default=default_idx)
    if choice == "q":
        return None
    return options[int(choice) - 1][0]


def ask(prompt: str, default: str = "", password: bool = False) -> str:
    return Prompt.ask(f"  {prompt}", default=default, password=password)


def confirm(prompt: str, default: bool = True) -> bool:
    return Confirm.ask(f"  {prompt}", default=default)


def quit_wizard() -> None:
    """Ask for confirmation then exit."""
    console.print()
    if Confirm.ask("  Really quit? Nothing will be saved.", default=False):
        console.print("[dim]  Setup cancelled.[/dim]")
        sys.exit(0)


def info(msg: str) -> None:
    rprint(f"  {msg}")


def warn(msg: str) -> None:
    rprint(f"  [yellow]{msg}[/yellow]")


def success(msg: str) -> None:
    rprint(f"  [green]✓[/green] {msg}")


def error(msg: str) -> None:
    rprint(f"  [red]✗[/red] {msg}")


def required_label(label: str, satisfied: bool) -> str:
    """Return a label with a red asterisk prefix when the requirement is unmet."""
    if satisfied:
        return f"[green]✓[/green]  {label}"
    return f"[bold red]*[/bold red]  {label}  [dim red](required)[/dim red]"


def show_dict_table(title: str, data: dict) -> None:
    """Render a simple key→value table."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    for k, v in data.items():
        table.add_row(str(k), str(v))
    console.print(f"  [bold]{title}[/bold]")
    console.print(table)
    console.print()
