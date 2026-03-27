"""Shared UI utilities for the onboarding wizard — retro terminal aesthetic."""

import sys
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich.text import Text
from rich.table import Table
from rich import box
from rich import print as rprint

console = Console()

# ---------------------------------------------------------------------------
# Colour palette — phosphor-green terminal
# ---------------------------------------------------------------------------

_PRI  = "bright_green"    # primary output
_DIM  = "green"            # dimmed / secondary
_ACC  = "cyan"             # accent / highlights
_WARN = "yellow"           # warnings / notices
_ERR  = "red"              # errors


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

# Small block-style wordmark (fits inside a standard 80-col panel)
_WORDMARK = (
    "[bold bright_green]"
    " ██████╗ ██╗   ██╗ ██████╗██╗      ██████╗ ██████╗ ███████╗███████╗\n"
    " ██╔══██╗╚██╗ ██╔╝██╔════╝██║     ██╔═══██╗██╔══██╗██╔════╝██╔════╝\n"
    " ██████╔╝ ╚████╔╝ ██║     ██║     ██║   ██║██████╔╝███████╗█████╗  \n"
    " ██╔═══╝   ╚██╔╝  ██║     ██║     ██║   ██║██╔═══╝ ╚════██║██╔══╝  \n"
    " ██║        ██║   ╚██████╗███████╗╚██████╔╝██║     ███████║███████╗\n"
    " ╚═╝        ╚═╝    ╚═════╝╚══════╝ ╚═════╝ ╚═╝     ╚══════╝╚══════╝"
    "[/bold bright_green]"
)


def header(data_dir) -> None:
    try:
        from pyclopse._version import __version__
        ver = f"v{__version__}"
    except Exception:
        ver = "DEV"

    body = Text.from_markup(_WORDMARK)
    body.append("\n")
    body.append("\n  AUTONOMOUS AGENT GATEWAY  ", style="bright_green")
    body.append(f" {ver} ", style="reverse bright_green")
    body.append("  ·  SETUP & CONFIGURATION", style="dim green")

    console.print()
    console.print(Panel(
        body,
        border_style="green",
        box=box.DOUBLE,
        padding=(0, 1),
        subtitle=Text.assemble(
            ("DATA DIR: ", "dim green"),
            (str(data_dir), "green"),
        ),
    ))
    console.print()


# ---------------------------------------------------------------------------
# Section dividers
# ---------------------------------------------------------------------------

def section(title: str, style: str = "cyan") -> None:
    colour = {
        "cyan":   "bright_green",
        "green":  "bright_green",
        "yellow": "yellow",
        "red":    "red",
    }.get(style, style)
    console.print()
    console.print(Rule(
        f"[ {title.upper()} ]",
        style=colour,
        characters="═",
    ))
    console.print()


# ---------------------------------------------------------------------------
# Menu / prompt helpers
# ---------------------------------------------------------------------------

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
        if is_default:
            default_idx = str(i)
        marker = "  [dim green]◄[/dim green]" if is_default else ""
        console.print(
            f"  [bold bright_green][[/bold bright_green]"
            f"[bold cyan]{i}[/bold cyan]"
            f"[bold bright_green]][/bold bright_green]"
            f"  {label}{marker}"
        )

    if allow_quit:
        console.print(
            f"  [bold bright_green][[/bold bright_green]"
            f"[bold cyan]Q[/bold cyan]"
            f"[bold bright_green]][/bold bright_green]"
            f"  [dim green]QUIT[/dim green]"
        )

    console.print()
    choices = [str(i) for i in range(1, len(options) + 1)]
    if allow_quit:
        choices.append("q")

    choice = Prompt.ask(
        f"  [bright_green]▶[/bright_green] {prompt}",
        choices=choices,
        default=default_idx,
    )
    if choice == "q":
        return None
    return options[int(choice) - 1][0]


def ask(prompt: str, default: str = "", password: bool = False) -> str:
    return Prompt.ask(
        f"  [bright_green]▶[/bright_green] {prompt}",
        default=default,
        password=password,
    )


def confirm(prompt: str, default: bool = True) -> bool:
    return Confirm.ask(
        f"  [bright_green]▶[/bright_green] {prompt}",
        default=default,
    )


def quit_wizard() -> None:
    """Ask for confirmation then exit."""
    console.print()
    if Confirm.ask(
        "  [yellow]▶[/yellow] REALLY QUIT? ALL UNSAVED CHANGES WILL BE LOST",
        default=False,
    ):
        console.print()
        console.print("[dim green]  ── SETUP CANCELLED.  GOODBYE. ──[/dim green]")
        console.print()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Status / feedback messages
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    rprint(f"  {msg}")


def warn(msg: str) -> None:
    rprint(f"  [yellow]▲[/yellow]  {msg}")


def success(msg: str) -> None:
    rprint(f"  [bright_green][OK][/bright_green]  {msg}")


def error(msg: str) -> None:
    rprint(f"  [red][ERR][/red]  {msg}")


def required_label(label: str, satisfied: bool) -> str:
    """Return a label with status indicator prefix."""
    if satisfied:
        return f"[bright_green][OK][/bright_green]  {label}"
    return f"[bold red][ * ][/bold red]  {label}  [dim red](REQUIRED)[/dim red]"


def show_dict_table(title: str, data: dict) -> None:
    """Render a simple key→value table."""
    table = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 2),
        border_style="green",
    )
    table.add_column(style="dim green")
    table.add_column(style="bright_green")
    for k, v in data.items():
        table.add_row(str(k), str(v))
    console.print(f"  [bold bright_green]{title}[/bold bright_green]")
    console.print(table)
    console.print()
