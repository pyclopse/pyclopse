"""Textual-based onboarding wizard — single-window TUI with mouse support."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Footer, Input, Label, ListItem, ListView,
    RadioButton, RadioSet, Static, Switch,
)

from .steps.provider import KNOWN_PROVIDERS

# ---------------------------------------------------------------------------
# Retro palette / CSS
# ---------------------------------------------------------------------------

_CSS = """
/* ── Base ── */
Screen {
    background: #060d06;
    color: #00cc44;
    layers: base overlay;
}

/* ── Logo header (docked top, present on every screen) ── */
LogoHeader {
    height: auto;
    dock: top;
    background: #030803;
    border-bottom: double #005522;
    padding: 0 1;
    overflow-x: hidden;
}

/* ── Section dividers ── */
.section-rule {
    height: 1;
    color: #00ff41;
    text-style: bold;
    background: #060d06;
    padding: 0 2;
    margin-top: 1;
}

/* ── Info / status text ── */
.info-text {
    color: #007733;
    padding: 0 2;
}
.warning-text {
    color: #ccaa00;
    padding: 0 2;
}
.ok-text {
    color: #00ff41;
    padding: 0 2;
}

/* ── ListView menus ── */
ListView {
    background: #030803;
    border: double #005522;
    margin: 1 2;
    padding: 0;
    height: auto;
    max-height: 18;
}
ListItem {
    padding: 0 1;
    height: 1;
    color: #00cc44;
    background: #030803;
}
ListItem:hover {
    background: #001a00;
    color: #00ff55;
}
ListItem.--highlight {
    background: #00cc44;
    color: #000d06;
    text-style: bold;
}

/* ── Form fields ── */
.field-label {
    padding: 0 2;
    margin-top: 1;
    color: #00aa44;
    text-style: bold;
}
.field-hint {
    padding: 0 2;
    color: #005522;
}
.validation-error {
    color: #cc4444;
    padding: 0 2;
    display: none;
}
Input {
    background: #030803;
    border: double #005522;
    color: #00ff41;
    margin: 0 2 0 2;
}
Input:focus {
    border: double #00aa33;
}

/* ── RadioSet ── */
RadioSet {
    background: #060d06;
    border: none;
    margin: 0 2;
    padding: 0;
    height: auto;
}
RadioButton {
    color: #00cc44;
    background: #060d06;
}
RadioButton:hover {
    color: #00ff55;
    background: #002800;
}
RadioButton.-on {
    color: #00ff41;
    text-style: bold;
}

/* ── Switch ── */
Switch {
    margin: 0 2;
}

/* ── Buttons ── */
Button {
    background: #002800;
    color: #00cc44;
    border: tall #005522;
    margin: 0 1;
    min-width: 22;
}
Button:focus {
    background: #00ff41;
    color: #000000;
    border: tall #00ff41;
    text-style: bold;
}
Button:hover {
    background: #004400;
    color: #00ff55;
}
Button.-primary {
    background: #003a00;
    border: tall #009933;
    color: #00ff41;
}
Button.-error {
    background: #200000;
    border: tall #880000;
    color: #cc4444;
}
Button.-warning {
    background: #1a1500;
    border: tall #776600;
    color: #ccaa00;
}
.button-row {
    height: 3;
    align: center middle;
    padding: 0 2;
}

/* ── Footer ── */
Footer {
    background: #002800;
    color: #00aa33;
}

/* ── Modal overlays ── */
ConfirmScreen, QuitConfirmScreen {
    align: center middle;
}
#confirm-dialog {
    width: 62;
    height: auto;
    background: #030803;
    border: double #009933;
    padding: 1 2;
}
#confirm-dialog ListView {
    margin: 1 0 0 0;
    height: auto;
    max-height: 4;
    border: none;
}

ProviderFormScreen, AgentFormScreen, TelegramFormScreen, SlackFormScreen {
    align: center middle;
}
#form-dialog {
    width: 76;
    height: auto;
    max-height: 40;
    background: #030803;
    border: double #009933;
    padding: 0 0 1 0;
}
.form-scroll {
    height: auto;
    max-height: 28;
    overflow-y: auto;
}
"""

# ---------------------------------------------------------------------------
# Logo block art + header widget
# ---------------------------------------------------------------------------

_LOGO_ART = (
    " ██████╗ ██╗   ██╗ ██████╗██╗      ██████╗ ██████╗ ███████╗███████╗\n"
    " ██╔══██╗╚██╗ ██╔╝██╔════╝██║     ██╔═══██╗██╔══██╗██╔════╝██╔════╝\n"
    " ██████╔╝ ╚████╔╝ ██║     ██║     ██║   ██║██████╔╝███████╗█████╗  \n"
    " ██╔═══╝   ╚██╔╝  ██║     ██║     ██║   ██║██╔═══╝ ╚════██║██╔══╝  \n"
    " ██║        ██║   ╚██████╗███████╗╚██████╔╝██║     ███████║███████╗\n"
    " ╚═╝        ╚═╝    ╚═════╝╚══════╝ ╚═════╝ ╚═╝     ╚══════╝╚══════╝"
)


class LogoHeader(Container):
    """Block-art logo + version/data-dir subheader, docked to the top of every screen."""

    def compose(self) -> ComposeResult:
        try:
            from pyclopse._version import __version__
            ver = f"v{__version__}"
        except Exception:
            ver = "DEV"
        app: WizardApp = self.app  # type: ignore[assignment]
        yield Static(f"[bold #00ff41]{_LOGO_ART}[/bold #00ff41]", markup=True)
        yield Static(
            f"[#00ff41]  AUTONOMOUS AGENT GATEWAY[/#00ff41]  "
            f"[reverse #00ff41] {ver} [/reverse #00ff41]  "
            f"[dim #007722]·  SETUP & CONFIGURATION  ·  {app.wiz_data_dir}[/dim #007722]",
            markup=True,
        )


# ---------------------------------------------------------------------------
# CaretItem / CaretListView — dynamic caret on the highlighted item only
# ---------------------------------------------------------------------------

class CaretItem(ListItem):
    """ListItem that shows a ► caret prefix only when highlighted."""

    def __init__(self, text: str, iid: str, danger: bool = False, **kw):
        super().__init__(id=iid, **kw)
        self._base   = text
        self._colour = "#dd2222" if danger else "#00cc44"

    def compose(self) -> ComposeResult:
        yield Label(
            f"[{self._colour}]  {self._base}[/{self._colour}]",
            markup=True,
        )

    def activate_caret(self, active: bool) -> None:
        prefix = "► " if active else "  "
        try:
            self.query_one(Label).update(
                f"[{self._colour}]{prefix}{self._base}[/{self._colour}]"
            )
        except Exception:
            pass


class CaretListView(ListView):
    """ListView that delegates ► caret management to its CaretItem children."""

    def on_mount(self) -> None:
        # Labels inside CaretItems may not be mounted yet when the first
        # Highlighted event fires; schedule a sync for after the next render.
        self.call_after_refresh(self._apply_carets)

    def _apply_carets(self) -> None:
        """Activate caret on the currently highlighted item; clear all others.

        If nothing is highlighted yet (e.g. after a clear+rebuild), force
        index to 0 so the first item gets the caret and the visual highlight.
        """
        if self.highlighted_child is None and self._nodes:
            self.index = 0
        highlighted = self.highlighted_child
        for node in self._nodes:
            if isinstance(node, CaretItem):
                node.activate_caret(node is highlighted)

    @on(ListView.Highlighted)
    def _sync_carets(self, event: ListView.Highlighted) -> None:
        if event.list_view is not self:
            return
        for node in self._nodes:
            if isinstance(node, CaretItem):
                node.activate_caret(node is event.item)


def _caret_item(text: str, iid: str, danger: bool = False) -> CaretItem:
    """Return a CaretItem — caret visible only when the item is highlighted."""
    return CaretItem(text, iid, danger)


# ---------------------------------------------------------------------------
# Generic confirm modal
# ---------------------------------------------------------------------------

class ConfirmScreen(ModalScreen[bool]):
    """Simple yes/no confirmation dialog — uses ListView for keyboard navigation."""

    def __init__(self, message: str, yes_label: str = "YES", no_label: str = "CANCEL", **kw):
        super().__init__(**kw)
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static(
                f"[bold #00ff41]{self._message}[/bold #00ff41]",
                markup=True,
            )
            yield CaretListView(
                _caret_item(f"  {self._yes_label}", "item-yes", danger=True),
                _caret_item(f"  {self._no_label}",  "item-no"),
                id="confirm-list",
            )

    def on_mount(self) -> None:
        self.query_one("#confirm-list").focus()

    @on(ListView.Selected, "#confirm-list")
    def on_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.id == "item-yes")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)


# ---------------------------------------------------------------------------
# Security screen
# ---------------------------------------------------------------------------

_SECURITY_TEXT = (
    "[bold yellow]▲  CAPABILITIES[/bold yellow]\n\n"
    "[yellow]     ●  Read and write files on your filesystem\n"
    "     ●  Execute shell commands (if exec tools are enabled)\n"
    "     ●  Send messages to connected channels on your behalf[/yellow]\n\n"
    "[bold #007733]─  BEST PRACTICES[/bold #007733]\n\n"
    "[#007733]     ●  Only allow users you trust in channel configs\n"
    "     ●  Keep API keys in secrets / .env, not in config.yaml\n"
    "     ●  Review exec_approvals before enabling shell tools[/#007733]\n"
)


class SecurityScreen(Screen):
    BINDINGS = [Binding("escape", "cancel", "Cancel Setup")]

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold yellow]═══════════════[ ! SECURITY ADVISORY ! ]═══════════════[/bold yellow]",
            markup=True,
            classes="section-rule",
        )
        yield Static(_SECURITY_TEXT, markup=True, classes="info-text")
        yield CaretListView(
            _caret_item("  I UNDERSTAND — CONTINUE WITH SETUP", "item-ack"),
            _caret_item("  CANCEL SETUP",                        "item-cancel", danger=True),
            id="security-menu",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#security-menu").focus()

    @on(ListView.Selected, "#security-menu")
    def on_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "item-ack":
            self.app.pop_screen()
            self.app.push_screen(MainMenuScreen())
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Main menu screen
# ---------------------------------------------------------------------------

def _provider_badge(config: dict) -> str:
    pids = list(config.get("providers", {}).keys())
    if pids:
        return f"[bright_green][OK][/bright_green]  PROVIDERS  [dim #007722]({', '.join(pids)})[/dim #007722]"
    return "[bold red][ * ][/bold red]  PROVIDERS  [dim red](REQUIRED)[/dim red]"


def _agent_badge(config: dict) -> str:
    aids = list(config.get("agents", {}).keys())
    if aids:
        return f"[bright_green][OK][/bright_green]  AGENTS  [dim #007722]({', '.join(aids)})[/dim #007722]"
    return "[bold red][ * ][/bold red]  AGENTS  [dim red](REQUIRED)[/dim red]"


def _channel_badge(config: dict) -> str:
    cids = list(config.get("channels", {}).keys())
    if cids:
        return f"[bright_green][OK][/bright_green]  CHANNELS  [dim #007722]({', '.join(cids)})[/dim #007722]"
    return "[dim #007722]       CHANNELS  (optional)[/dim #007722]"


class MainMenuScreen(Screen):
    BINDINGS = [Binding("escape", "noop", show=False)]

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold #00ff41]═══════════════[ SYSTEM CONFIGURATION ]═══════════════[/bold #00ff41]",
            markup=True,
            classes="section-rule",
        )
        yield Static(
            "[dim #007722]  Items marked [bold red][ * ][/bold red][dim #007722] are required before you can save.[/dim #007722]",
            markup=True,
            classes="info-text",
        )
        yield CaretListView(id="main-menu")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild()

    def on_screen_resume(self) -> None:
        self._rebuild()

    @work(exclusive=True)
    async def _rebuild(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        cfg = app.wiz_config
        has_p = bool(cfg.get("providers"))
        has_a = bool(cfg.get("agents"))
        can_save = has_p and has_a

        lv = self.query_one("#main-menu", CaretListView)
        await lv.clear()
        lv.append(_caret_item(_provider_badge(cfg), "item-providers"))
        lv.append(_caret_item(_agent_badge(cfg),    "item-agents"))
        lv.append(_caret_item(_channel_badge(cfg),  "item-channels"))
        if can_save:
            lv.append(_caret_item("[bright_green]★  SAVE AND INITIALIZE[/bright_green]", "item-save"))
        lv.index = 0
        lv.call_after_refresh(lv._apply_carets)

    @on(ListView.Selected, "#main-menu")
    def on_selected(self, event: ListView.Selected) -> None:
        iid = event.item.id
        app: WizardApp = self.app  # type: ignore[assignment]
        if iid == "item-providers":
            app.push_screen(ProviderListScreen())
        elif iid == "item-agents":
            if not app.wiz_config.get("providers"):
                app.push_screen(ConfirmScreen(
                    "Configure at least one provider first.",
                    yes_label="OK", no_label="OK",
                ))
            else:
                app.push_screen(AgentListScreen())
        elif iid == "item-channels":
            app.push_screen(ChannelListScreen())
        elif iid == "item-save":
            self._do_save()

    def _do_save(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        cfg = app.wiz_config
        if "gateway" not in cfg:
            cfg["gateway"] = {"host": "0.0.0.0", "port": 8080, "log_level": "info"}
        if "version" not in cfg:
            cfg["version"] = "1.0"
        app.push_screen(SummaryScreen())

    def action_noop(self) -> None:
        pass  # Escape does nothing on the main menu (use q to quit)


# ---------------------------------------------------------------------------
# Provider list + form
# ---------------------------------------------------------------------------

class ProviderFormScreen(ModalScreen[dict | None]):
    """Add or edit a single provider."""

    def __init__(self, provider_id: str | None = None, existing: dict | None = None, **kw):
        super().__init__(**kw)
        self._pid = provider_id        # None = new
        self._existing = existing or {}

    def compose(self) -> ComposeResult:
        title = f"EDIT PROVIDER: {self._pid}" if self._pid else "ADD PROVIDER"
        with Container(id="form-dialog"):
            yield Static(
                f"[bold #00ff41]═══[ {title} ]═══[/bold #00ff41]",
                markup=True, classes="section-rule",
            )
            with ScrollableContainer(classes="form-scroll"):
                if not self._pid:
                    yield Static("[bold #00aa44]PROVIDER TYPE[/bold #00aa44]", markup=True, classes="field-label")
                    buttons = [
                        RadioButton(pdef["label"], id=f"p-{pid}", value=(i == 0))
                        for i, (pid, pdef) in enumerate(KNOWN_PROVIDERS.items())
                    ]
                    yield RadioSet(*buttons, id="provider-type")
                    yield Static("[bold #00aa44]PROVIDER ID  [dim](for custom only)[/dim][/bold #00aa44]",
                                 markup=True, classes="field-label", id="custom-id-label")
                    yield Input(placeholder="e.g.  my-ollama", id="custom-id")

                key_name = self._get_pdef().get("key_name", "API_KEY")
                hint = self._get_pdef().get("key_hint", "")
                yield Static(f"[bold #00aa44]API KEY[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Static(f"[dim #005522]  Hint: {hint}[/dim #005522]",
                             markup=True, classes="field-hint", id="api-key-hint")
                yield Input(placeholder=hint, password=True, id="api-key")

                yield Static("[bold #00aa44]BASE URL[/bold #00aa44]",
                             markup=True, classes="field-label", id="url-label")
                first_pdef = self._get_pdef()
                existing_url = self._existing.get("api_url", first_pdef.get("default_url") or "")
                yield Input(value=existing_url, id="api-url")

                existing_models = self._existing.get("models", {})
                pdef = self._get_pdef()
                model_hint = ", ".join(pdef.get("default_models", [])) or "e.g. my-model"
                default_val = ", ".join(existing_models.keys()) if existing_models else pdef.get("default_model", "")
                yield Static("[bold #00aa44]MODELS  [dim](comma-separated)[/dim][/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Static(f"[dim #005522]  Available: {model_hint}[/dim #005522]",
                             markup=True, classes="field-hint", id="model-hint")
                yield Input(value=default_val, id="models")

                yield Static("", classes="field-label", id="validation-err")

            with Horizontal(classes="button-row"):
                yield Button("FETCH MODELS", id="btn-fetch", variant="default")
                yield Button("SAVE", id="btn-save", variant="primary")
                yield Button("CANCEL", id="btn-cancel")

    def on_mount(self) -> None:
        """Set initial field visibility after the DOM is ready."""
        needs_url = self._needs_url()
        self.query_one("#url-label").display = needs_url
        self.query_one("#api-url").display = needs_url
        if not self._pid:
            # custom-id only shown for the 'generic' catch-all
            self.query_one("#custom-id-label").display = False
            self.query_one("#custom-id").display = False

    def _current_pid(self) -> str:
        if self._pid:
            return self._pid
        rs = self.query_one("#provider-type", RadioSet)
        idx = rs.pressed_index
        keys = list(KNOWN_PROVIDERS.keys())
        if idx < len(keys):
            pid = keys[idx]
        else:
            pid = "generic"
        if pid == "generic":
            custom = self.query_one("#custom-id", Input).value.strip()
            if custom:
                return custom
        return pid

    def _get_pdef(self, pid: str | None = None) -> dict:
        p = pid or self._pid or "anthropic"
        return KNOWN_PROVIDERS.get(p, KNOWN_PROVIDERS["generic"])

    def _needs_url(self, pid: str | None = None) -> bool:
        return self._get_pdef(pid).get("needs_url", False)

    @on(RadioSet.Changed, "#provider-type")
    def on_type_changed(self, event: RadioSet.Changed) -> None:
        keys = list(KNOWN_PROVIDERS.keys())
        idx = event.index
        pid = keys[idx] if idx < len(keys) else "generic"
        pdef = KNOWN_PROVIDERS.get(pid, KNOWN_PROVIDERS["generic"])
        needs_url = pdef.get("needs_url", False)
        is_custom = (pid == "generic")

        self.query_one("#url-label").display = needs_url
        self.query_one("#api-url").display = needs_url
        self.query_one("#custom-id-label").display = is_custom
        self.query_one("#custom-id").display = is_custom

        # Pre-fill URL with provider default
        if needs_url:
            self.query_one("#api-url", Input).value = pdef.get("default_url") or ""

        # Update key hint
        self.query_one("#api-key-hint", Static).update(
            f"[dim #005522]  Hint: {pdef.get('key_hint', '')}[/dim #005522]"
        )
        # Update model hint and default
        model_list = ", ".join(pdef.get("default_models", [])) or "e.g. my-model"
        self.query_one("#model-hint", Static).update(
            f"[dim #005522]  Available: {model_list}[/dim #005522]"
        )
        self.query_one("#models", Input).value = pdef.get("default_model", "")

    @on(Button.Pressed, "#btn-fetch")
    def on_fetch_models(self) -> None:
        """Fetch available models from the provider's /models endpoint."""
        from .steps.provider import _fetch_available_models
        api_key = self.query_one("#api-key", Input).value.strip()
        api_url = self.query_one("#api-url", Input).value.strip()
        err = self.query_one("#validation-err", Static)
        if not api_key or not api_url:
            err.update("[bold yellow]  Enter API key and URL first.[/bold yellow]")
            err.display = True
            return

        hint = self.query_one("#model-hint", Static)
        hint.update("[dim #005522]  Fetching models...[/dim #005522]")
        err.display = False

        def do_fetch() -> list[str] | None:
            return _fetch_available_models(api_url, api_key)

        self.run_worker(do_fetch, exclusive=True, thread=True, name="fetch-models")

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        if event.worker.name == "fetch-models" and event.state == WorkerState.SUCCESS:
            models = event.worker.result
            hint = self.query_one("#model-hint", Static)
            if models:
                self.query_one("#models", Input).value = ", ".join(models)
                hint.update(f"[dim #005522]  Found {len(models)} model(s) — edit as needed.[/dim #005522]")
            else:
                hint.update("[dim #005522]  Could not fetch — enter models manually.[/dim #005522]")

    @on(Button.Pressed, "#btn-save")
    def on_save(self) -> None:
        pid = self._current_pid()
        api_key = self.query_one("#api-key", Input).value.strip()
        models_raw = self.query_one("#models", Input).value.strip()

        err = self.query_one("#validation-err", Static)
        if not api_key and not self._existing.get("api_key"):
            err.update("[bold red]  API key is required.[/bold red]")
            err.display = True
            return
        if not models_raw:
            err.update("[bold red]  At least one model is required.[/bold red]")
            err.display = True
            return
        err.display = False

        pdef = self._get_pdef(pid if pid not in KNOWN_PROVIDERS else pid)
        key_name = pdef.get("key_name", f"{pid.upper()}_API_KEY")
        concurrency = pdef.get("default_concurrency", 5)

        # Build config blob
        cfg: dict[str, Any] = {"enabled": True}
        secrets: dict[str, Any] = {}
        env: dict[str, Any] = {}

        if api_key and api_key.lower() != "none":
            env[key_name] = api_key
            secrets[key_name] = {"source": "env"}

        if pid in ("anthropic", "openai"):
            cfg["apiKey"] = f"${{{key_name}}}"
        else:
            cfg["fastagent_provider"] = pdef.get("fastagent_provider", "generic")
            cfg["api_key"] = f"${{{key_name}}}" if api_key else self._existing.get("api_key", "none")
            cfg["api_url"] = self.query_one("#api-url", Input).value.strip() or "http://localhost:11434/v1"

        selected = [m.strip() for m in models_raw.split(",") if m.strip()]
        cfg["models"] = {
            m: {"enabled": True, "concurrency": self._existing.get("models", {}).get(m, {}).get("concurrency", concurrency)}
            for m in selected
        }

        self.dismiss({"pid": pid, "cfg": cfg, "secrets": secrets, "env": env})

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ProviderListScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("d", "delete_selected", "Delete", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold #00ff41]═══════════════[ CONFIGURE PROVIDERS ]═══════════════[/bold #00ff41]",
            markup=True, classes="section-rule",
        )
        yield Static(
            "[dim #007722]  Select a provider to edit · [bold]D[/bold] to delete highlighted[/dim #007722]",
            markup=True, classes="info-text",
        )
        yield CaretListView(id="provider-list")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild()

    def on_screen_resume(self) -> None:
        self._rebuild()

    @work(exclusive=True)
    async def _rebuild(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        lv = self.query_one("#provider-list", CaretListView)
        await lv.clear()
        for pid, pcfg in app.wiz_config.get("providers", {}).items():
            label = KNOWN_PROVIDERS.get(pid, {}).get("label", pid)
            models = ", ".join(pcfg.get("models", {}).keys()) or "none"
            lv.append(_caret_item(
                f"[bright_green][OK][/bright_green]  {pid}"
                f"  [dim #007722]({label})[/dim #007722]"
                f"  models: [#00cc44]{models}[/#00cc44]",
                f"pid-{pid}",
            ))
        lv.append(_caret_item("  [+]  ADD PROVIDER", "item-add"))
        lv.append(_caret_item("  [◄]  DONE", "item-done", danger=True))
        lv.index = 0
        lv.call_after_refresh(lv._apply_carets)

    @on(ListView.Selected, "#provider-list")
    def on_selected(self, event: ListView.Selected) -> None:
        iid = event.item.id
        if iid == "item-add":
            self.app.push_screen(ProviderFormScreen(), self._on_form_result)
        elif iid == "item-done":
            self.action_go_back()
        elif iid and iid.startswith("pid-"):
            pid = iid.removeprefix("pid-")
            existing = self.app.wiz_config.get("providers", {}).get(pid, {})
            self.app.push_screen(ProviderFormScreen(provider_id=pid, existing=existing), self._on_form_result)

    def action_delete_selected(self) -> None:
        lv = self.query_one("#provider-list", CaretListView)
        if lv.highlighted_child is None:
            return
        iid = lv.highlighted_child.id
        if iid and iid.startswith("pid-"):
            pid = iid.removeprefix("pid-")
            self.app.push_screen(
                ConfirmScreen(f"REMOVE PROVIDER '{pid}'?", yes_label="REMOVE", no_label="CANCEL"),
                lambda yes: self._do_remove(pid, yes),
            )

    def _do_remove(self, pid: str, confirmed: bool) -> None:
        if confirmed:
            self.app.wiz_config.get("providers", {}).pop(pid, None)
            self._rebuild()

    def _on_form_result(self, result: dict | None) -> None:
        if result:
            app: WizardApp = self.app  # type: ignore[assignment]
            pid = result["pid"]
            app.wiz_config.setdefault("providers", {})[pid] = result["cfg"]
            app.wiz_secrets.update(result["secrets"])
            app.wiz_env.update(result["env"])
            self._rebuild()

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Agent list + form
# ---------------------------------------------------------------------------

class AgentFormScreen(ModalScreen[dict | None]):
    """Add or edit a single agent."""

    def __init__(self, agent_id: str | None = None, existing: dict | None = None, **kw):
        super().__init__(**kw)
        self._aid = agent_id
        self._existing = existing or {}

    def compose(self) -> ComposeResult:
        title = f"EDIT AGENT: {self._aid}" if self._aid else "ADD AGENT"
        app: WizardApp = self.app  # type: ignore[assignment]
        model_strings = [
            f"{pid}/{mid}"
            for pid, pcfg in app.wiz_config.get("providers", {}).items()
            for mid in pcfg.get("models", {}).keys()
        ]
        model_hint = "\n     ".join(model_strings) if model_strings else "e.g. anthropic/claude-sonnet-4-6"
        default_model = self._existing.get("model", model_strings[0] if model_strings else "")
        default_mcps = ", ".join(
            self._existing.get("mcp_servers", ["pyclopse", "fetch", "time", "filesystem"])
        )
        with Container(id="form-dialog"):
            yield Static(
                f"[bold #00ff41]═══[ {title} ]═══[/bold #00ff41]",
                markup=True, classes="section-rule",
            )
            with ScrollableContainer(classes="form-scroll"):
                if not self._aid:
                    yield Static("[bold #00aa44]AGENT ID[/bold #00aa44]",
                                 markup=True, classes="field-label")
                    yield Static("[dim #005522]  e.g.  main, assistant, coder[/dim #005522]",
                                 markup=True, classes="field-hint")
                    yield Input(placeholder="main", id="agent-id")

                yield Static("[bold #00aa44]DISPLAY NAME[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(value=self._existing.get("name", ""), placeholder="Main Agent", id="agent-name")

                yield Static("[bold #00aa44]MODEL[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Static(f"[dim #005522]  Available: {model_hint}[/dim #005522]",
                             markup=True, classes="field-hint")
                yield Input(value=default_model, placeholder="anthropic/claude-sonnet-4-6", id="agent-model")

                yield Static("[bold #00aa44]MCP SERVERS  [dim](comma-separated)[/dim][/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(value=default_mcps, id="agent-mcps")

                yield Static("[bold #00aa44]SHOW THINKING BLOCKS TO USERS?[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Switch(value=self._existing.get("show_thinking", False), id="agent-thinking")

                yield Static("", classes="field-label", id="validation-err")

            with Horizontal(classes="button-row"):
                yield Button("SAVE", id="btn-save", variant="primary")
                yield Button("CANCEL", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def on_save(self) -> None:
        err = self.query_one("#validation-err", Static)

        # Agent ID
        if self._aid:
            aid = self._aid
        else:
            raw = self.query_one("#agent-id", Input).value.strip().lower().replace(" ", "_")
            aid = "".join(c for c in raw if c.isalnum() or c == "_")
            if not aid:
                err.update("[bold red]  Invalid agent ID.[/bold red]")
                err.display = True
                return

        name = self.query_one("#agent-name", Input).value.strip() or aid.capitalize()
        model = self.query_one("#agent-model", Input).value.strip()
        if not model:
            err.update("[bold red]  Model is required.[/bold red]")
            err.display = True
            return
        err.display = False

        mcps_raw = self.query_one("#agent-mcps", Input).value.strip()
        mcps = [s.strip() for s in mcps_raw.split(",") if s.strip()]
        show_thinking = self.query_one("#agent-thinking", Switch).value

        from typing import Any as _Any
        cfg: dict[str, _Any] = {
            "name": name,
            "model": model,
            "contextWindow": self._existing.get("contextWindow", 200000),
            "use_fastagent": True,
            "show_thinking": show_thinking,
            "mcp_servers": mcps,
        }
        for k in ("vault", "queue", "a2a", "request_params", "tools", "skills_dirs",
                  "max_iterations", "max_tokens"):
            if k in self._existing:
                cfg[k] = self._existing[k]

        self.dismiss({"aid": aid, "cfg": cfg})

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class AgentListScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("d", "delete_selected", "Delete", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold #00ff41]═══════════════[ CONFIGURE AGENTS ]═══════════════[/bold #00ff41]",
            markup=True, classes="section-rule",
        )
        yield Static(
            "[dim #007722]  Select an agent to edit · [bold]D[/bold] to delete highlighted[/dim #007722]",
            markup=True, classes="info-text",
        )
        yield CaretListView(id="agent-list")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild()

    def on_screen_resume(self) -> None:
        self._rebuild()

    @work(exclusive=True)
    async def _rebuild(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        lv = self.query_one("#agent-list", CaretListView)
        await lv.clear()
        for aid, acfg in app.wiz_config.get("agents", {}).items():
            lv.append(_caret_item(
                f"[bright_green][OK][/bright_green]  {aid}"
                f"  [dim #007722]{acfg.get('name', aid)}[/dim #007722]"
                f"  ·  [#00cc44]{acfg.get('model', '?')}[/#00cc44]",
                f"aid-{aid}",
            ))
        lv.append(_caret_item("  [+]  ADD AGENT", "item-add"))
        lv.append(_caret_item("  [◄]  DONE", "item-done", danger=True))
        lv.index = 0
        lv.call_after_refresh(lv._apply_carets)

    @on(ListView.Selected, "#agent-list")
    def on_selected(self, event: ListView.Selected) -> None:
        iid = event.item.id
        if iid == "item-add":
            self.app.push_screen(AgentFormScreen(), self._on_result)
        elif iid == "item-done":
            self.action_go_back()
        elif iid and iid.startswith("aid-"):
            aid = iid.removeprefix("aid-")
            existing = self.app.wiz_config.get("agents", {}).get(aid, {})
            self.app.push_screen(AgentFormScreen(agent_id=aid, existing=existing), self._on_result)

    def action_delete_selected(self) -> None:
        lv = self.query_one("#agent-list", CaretListView)
        if lv.highlighted_child is None:
            return
        iid = lv.highlighted_child.id
        if iid and iid.startswith("aid-"):
            aid = iid.removeprefix("aid-")
            self.app.push_screen(
                ConfirmScreen(f"REMOVE AGENT '{aid}'?", yes_label="REMOVE", no_label="CANCEL"),
                lambda yes: self._do_remove(aid, yes),
            )

    def _do_remove(self, aid: str, confirmed: bool) -> None:
        if confirmed:
            self.app.wiz_config.get("agents", {}).pop(aid, None)
            self._rebuild()

    def _on_result(self, result: dict | None) -> None:
        if result:
            self.app.wiz_config.setdefault("agents", {})[result["aid"]] = result["cfg"]
            self._rebuild()

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Channel screens
# ---------------------------------------------------------------------------

class TelegramFormScreen(ModalScreen[dict | None]):
    def __init__(self, existing: dict | None = None, **kw):
        super().__init__(**kw)
        self._existing = existing or {}

    def compose(self) -> ComposeResult:
        app: WizardApp = self.app  # type: ignore[assignment]
        agent_ids = list(app.wiz_config.get("agents", {}).keys())
        default_agent = agent_ids[0] if agent_ids else "main"
        bots = self._existing.get("bots", {})
        with Container(id="form-dialog"):
            yield Static(
                "[bold #00ff41]═══[ CONFIGURE TELEGRAM ]═══[/bold #00ff41]",
                markup=True, classes="section-rule",
            )
            with ScrollableContainer(classes="form-scroll"):
                yield Static("[bold #00aa44]BOT NAME  [dim](label)[/dim][/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(value="main", id="bot-name")

                yield Static("[bold #00aa44]BOT TOKEN  [dim](from @BotFather)[/dim][/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(password=True, placeholder="123456:ABC-...", id="bot-token")

                yield Static("[bold #00aa44]AGENT[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(value=default_agent, id="bot-agent")

                yield Static("[bold #00aa44]ENABLE STREAMING?[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Switch(value=self._existing.get("streaming", True), id="streaming")

                yield Static("", classes="field-label", id="validation-err")

            with Horizontal(classes="button-row"):
                yield Button("SAVE", id="btn-save", variant="primary")
                yield Button("CANCEL", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def on_save(self) -> None:
        token = self.query_one("#bot-token", Input).value.strip()
        if not token:
            self.query_one("#validation-err", Static).update("[bold red]  Bot token is required.[/bold red]")
            self.query_one("#validation-err").display = True
            return
        bot_name = self.query_one("#bot-name", Input).value.strip() or "main"
        agent = self.query_one("#bot-agent", Input).value.strip() or "main"
        streaming = self.query_one("#streaming", Switch).value

        key = "TELEGRAM_BOT_TOKEN"
        env = {key: token}
        secrets = {key: {"source": "env"}}
        cfg = {
            "enabled": True,
            "streaming": streaming,
            "bots": {bot_name: {"botToken": f"${{{key}}}", "agent": agent}},
        }
        self.dismiss({"cfg": cfg, "secrets": secrets, "env": env})

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class SlackFormScreen(ModalScreen[dict | None]):
    def __init__(self, existing: dict | None = None, **kw):
        super().__init__(**kw)
        self._existing = existing or {}

    def compose(self) -> ComposeResult:
        app: WizardApp = self.app  # type: ignore[assignment]
        agent_ids = list(app.wiz_config.get("agents", {}).keys())
        default_agent = agent_ids[0] if agent_ids else "main"
        with Container(id="form-dialog"):
            yield Static(
                "[bold #00ff41]═══[ CONFIGURE SLACK ]═══[/bold #00ff41]",
                markup=True, classes="section-rule",
            )
            with ScrollableContainer(classes="form-scroll"):
                yield Static("[dim #007722]  You need a Bot Token (xoxb-...) and App Token (xapp-...)[/dim #007722]",
                             markup=True, classes="field-hint")
                yield Static("[bold #00aa44]BOT TOKEN  (xoxb-...)[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(password=True, placeholder="xoxb-...", id="bot-token")

                yield Static("[bold #00aa44]APP TOKEN  (xapp-...)[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(password=True, placeholder="xapp-...", id="app-token")

                yield Static("[bold #00aa44]AGENT[/bold #00aa44]",
                             markup=True, classes="field-label")
                yield Input(value=default_agent, id="slack-agent")

                yield Static("", classes="field-label", id="validation-err")

            with Horizontal(classes="button-row"):
                yield Button("SAVE", id="btn-save", variant="primary")
                yield Button("CANCEL", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def on_save(self) -> None:
        bot = self.query_one("#bot-token", Input).value.strip()
        app_tok = self.query_one("#app-token", Input).value.strip()
        if not bot or not app_tok:
            self.query_one("#validation-err", Static).update("[bold red]  Both tokens are required.[/bold red]")
            self.query_one("#validation-err").display = True
            return
        agent = self.query_one("#slack-agent", Input).value.strip() or "main"
        env = {"SLACK_BOT_TOKEN": bot, "SLACK_APP_TOKEN": app_tok}
        secrets = {k: {"source": "env"} for k in env}
        cfg = {
            "enabled": True,
            "botToken": "${SLACK_BOT_TOKEN}",
            "appToken": "${SLACK_APP_TOKEN}",
            "agent": agent,
        }
        self.dismiss({"cfg": cfg, "secrets": secrets, "env": env})

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ChannelListScreen(Screen):
    BINDINGS = [Binding("escape", "go_back", "Back")]

    _CHANNEL_SCREENS = {"telegram": TelegramFormScreen, "slack": SlackFormScreen}

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold #00ff41]═══════════════[ CONFIGURE CHANNELS ]═══════════════[/bold #00ff41]",
            markup=True, classes="section-rule",
        )
        yield Static(
            "[dim #007722]  Channels are optional — skip to use TUI/HTTP API only.[/dim #007722]",
            markup=True, classes="info-text",
        )
        yield CaretListView(id="channel-list")
        with Horizontal(classes="button-row"):
            yield Button("ADD / RECONFIGURE", id="btn-add", variant="primary")
            yield Button("REMOVE", id="btn-remove", variant="error")
            yield Button("DONE",   id="btn-done",   variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild()

    def on_screen_resume(self) -> None:
        self._rebuild()

    @work(exclusive=True)
    async def _rebuild(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        channels = app.wiz_config.get("channels", {})
        lv = self.query_one("#channel-list", CaretListView)
        await lv.clear()

        # available to add
        for cid in ("telegram", "slack"):
            status = "[bright_green][OK][/bright_green]" if cid in channels else "[dim #007722][ ][/dim #007722]"
            lv.append(_caret_item(f"{status}  {cid.upper()}", f"ch-{cid}"))

        self.query_one("#btn-remove").display = bool(channels)
        lv.index = 0
        lv.call_after_refresh(lv._apply_carets)

    def _selected_cid(self) -> str | None:
        lv = self.query_one("#channel-list", CaretListView)
        if lv.highlighted_child is None:
            return None
        return lv.highlighted_child.id.removeprefix("ch-")  # type: ignore[union-attr]

    @on(Button.Pressed, "#btn-add")
    def on_add(self) -> None:
        cid = self._selected_cid()
        if cid and cid in self._CHANNEL_SCREENS:
            existing = self.app.wiz_config.get("channels", {}).get(cid)
            self.app.push_screen(self._CHANNEL_SCREENS[cid](existing=existing), self._on_result(cid))

    def _on_result(self, cid: str):
        def _handler(result: dict | None) -> None:
            if result:
                self.app.wiz_config.setdefault("channels", {})[cid] = result["cfg"]
                self.app.wiz_secrets.update(result["secrets"])
                self.app.wiz_env.update(result["env"])
                self._rebuild()
        return _handler

    @on(Button.Pressed, "#btn-remove")
    def on_remove(self) -> None:
        cid = self._selected_cid()
        if cid:
            self.app.push_screen(
                ConfirmScreen(f"REMOVE CHANNEL '{cid}'?", yes_label="REMOVE", no_label="CANCEL"),
                lambda yes: self._do_remove(cid, yes),
            )

    def _do_remove(self, cid: str, confirmed: bool) -> None:
        if confirmed:
            self.app.wiz_config.get("channels", {}).pop(cid, None)
            self._rebuild()

    @on(Button.Pressed, "#btn-done")
    def action_go_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Summary / completion screen
# ---------------------------------------------------------------------------

class SummaryScreen(Screen):
    BINDINGS = [Binding("escape", "noop", show=False)]

    def compose(self) -> ComposeResult:
        yield LogoHeader()
        yield Static(
            "[bold bright_green]═══════════════[ SYSTEM READY ]═══════════════[/bold bright_green]",
            markup=True, classes="section-rule",
        )
        yield Static(id="summary-body", markup=True, classes="info-text")
        yield CaretListView(
            _caret_item("  ★  LAUNCH PYCLOPSE", "item-launch"),
            _caret_item("  ★  INSTALL AS SERVICE + LAUNCH TUI", "item-service"),
            _caret_item("  EXIT",               "item-exit",   danger=True),
            id="summary-menu",
        )
        yield Footer()

    def on_mount(self) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        dd = app.wiz_data_dir
        cfg = app.wiz_config

        lines = (
            f"\n"
            f"  [bright_green][OK][/bright_green]  CONFIG   {dd}/config/pyclopse.yaml\n"
            f"  [bright_green][OK][/bright_green]  SECRETS  {dd}/secrets/secrets.yaml\n"
            f"  [bright_green][OK][/bright_green]  ENV      {dd}/.env\n"
            f"\n"
            f"  [bold bright_green]REGISTERED AGENTS[/bold bright_green]\n"
        )
        for aid, acfg in cfg.get("agents", {}).items():
            lines += f"    [bright_green]●[/bright_green]  {acfg.get('name', aid)}  [dim #007722]({aid})[/dim #007722]  ·  {acfg.get('model', '?')}\n"

        lines += (
            f"\n  [bold bright_green]LAUNCH OPTIONS[/bold bright_green]\n"
            f"    [cyan]LAUNCH PYCLOPSE[/cyan]              Start gateway + TUI in this terminal\n"
            f"    [cyan]INSTALL AS SERVICE[/cyan]           Run as background service (starts on login)\n"
            f"                                 then connect dashboard with [cyan]pyclopse tui[/cyan]\n"
        )

        self.query_one("#summary-body", Static).update(lines)
        self.query_one("#summary-menu").focus()

    @on(ListView.Selected, "#summary-menu")
    def on_selected(self, event: ListView.Selected) -> None:
        app: WizardApp = self.app  # type: ignore[assignment]
        if event.item.id == "item-launch":
            app.launch_mode = "embedded"
            app.exit()
        elif event.item.id == "item-service":
            app.launch_mode = "service"
            app.exit()
        else:
            app.launch_mode = "none"
            self.app.exit()

    def action_noop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------

class WizardApp(App):
    CSS = _CSS
    TITLE = "pyclopse · setup"
    BINDINGS = [
        Binding("q", "quit_confirm", "Quit", show=True),
    ]

    def __init__(self, data_dir: Path, config: dict, secrets: dict, env: dict, fresh: bool):
        super().__init__()
        self.wiz_data_dir = data_dir
        self.wiz_config   = config
        self.wiz_secrets  = secrets
        self.wiz_env      = env
        self.wiz_fresh    = fresh
        self.launch_mode = "none"  # "none", "embedded", or "service"

    def on_mount(self) -> None:
        if self.wiz_fresh:
            self.push_screen(SecurityScreen())
        else:
            self.push_screen(MainMenuScreen())

    def action_quit_confirm(self) -> None:
        self.push_screen(
            ConfirmScreen("REALLY QUIT?  UNSAVED CHANGES WILL BE LOST.", yes_label="YES, QUIT"),
            lambda confirmed: self.exit() if confirmed else None,
        )


# ---------------------------------------------------------------------------
# Entry point called from wizard.py
# ---------------------------------------------------------------------------

def run_tui_wizard(
    data_dir: Path,
    config: dict,
    secrets: dict,
    env: dict,
    fresh: bool,
) -> tuple[dict, dict, dict, bool]:
    """Run the TUI wizard and return (config, secrets, env, launch_mode).

    Returns the accumulated wizard state after the user saves and exits.
    launch_mode is one of: "none", "embedded", "service".
    If the user quits without saving, returns the original inputs unchanged
    and launch_mode="none".
    """
    app = WizardApp(data_dir, config, secrets, env, fresh)
    app.run()
    return app.wiz_config, app.wiz_secrets, app.wiz_env, app.launch_mode
