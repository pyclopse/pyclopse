"""Tests for AgentRunner._build_fa_settings() — programmatic FastAgent config."""

import pytest
from unittest.mock import MagicMock
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_pyclawops_config(
    mcp_port: int = 8081,
    timezone: str = None,
    anthropic_key: str = None,
    openai_key: str = None,
    google_key: str = None,
    generic_key: str = None,
    generic_url: str = None,
    chrome_enabled: bool = False,
    chrome_auto_connect: bool = False,
    chrome_browser_url: str = None,
):
    cfg = MagicMock()
    cfg.gateway.mcp_port = mcp_port
    cfg.timezone = timezone

    cfg.providers.anthropic = MagicMock(api_key=anthropic_key, fastagent_provider=None) if anthropic_key else None
    cfg.providers.openai = MagicMock(api_key=openai_key, fastagent_provider=None) if openai_key else None
    cfg.providers.google = MagicMock(api_key=google_key, fastagent_provider=None) if google_key else None
    cfg.providers.minimax = (
        MagicMock(api_key=generic_key, api_url=generic_url, fastagent_provider="generic")
        if generic_key else None
    )
    cfg.providers.fastagent = None

    cdp = MagicMock()
    cdp.enabled = chrome_enabled
    cdp.auto_connect = chrome_auto_connect
    cdp.browser_url = chrome_browser_url
    cdp.headless = False
    cdp.channel = None
    cdp.executable_path = None
    cdp.slim = False
    cdp.package = "chrome-devtools-mcp@latest"
    cdp.command = "npx"
    cfg.browser.chrome_devtools_mcp = cdp
    return cfg


def _make_runner(servers=None, pyclawops_config=None, model="sonnet",
                 api_key=None, base_url=None, session_id=None):
    from pyclawops.agents.runner import AgentRunner
    r = AgentRunner.__new__(AgentRunner)
    r.agent_name = "test-agent"
    r.owner_name = "main"
    r.session_id = session_id
    r._log_prefix = "[main]"
    r.model = model
    r.servers = servers if servers is not None else ["pyclawops"]
    r.pyclawops_config = pyclawops_config
    r.api_key = api_key
    r.base_url = base_url
    return r


# ── pyclawops server URL ─────────────────────────────────────────────────────────

def test_pyclawops_server_url_from_mcp_port():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(mcp_port=9999))
    settings = runner._build_fa_settings()
    assert settings.mcp.servers["pyclawops"].url == "http://localhost:9999/mcp"


def test_pyclawops_server_default_port_without_config():
    runner = _make_runner(pyclawops_config=None)
    settings = runner._build_fa_settings()
    assert settings.mcp.servers["pyclawops"].url == "http://localhost:8081/mcp"


def test_pyclawops_server_headers_include_agent_name():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert settings.mcp.servers["pyclawops"].headers["x-agent-name"] == "main"


def test_pyclawops_server_headers_include_session_id():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(), session_id="sess-123abc")
    settings = runner._build_fa_settings()
    assert settings.mcp.servers["pyclawops"].headers["x-session-id"] == "sess-123abc"


def test_pyclawops_server_no_session_id_header_omitted():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(), session_id=None)
    settings = runner._build_fa_settings()
    assert "x-session-id" not in (settings.mcp.servers["pyclawops"].headers or {})


# ── standard servers ──────────────────────────────────────────────────────────

def test_fetch_server_defined_when_requested():
    runner = _make_runner(servers=["pyclawops", "fetch"], pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert "fetch" in settings.mcp.servers
    assert settings.mcp.servers["fetch"].command == "uvx"


def test_fetch_server_not_defined_when_not_requested():
    runner = _make_runner(servers=["pyclawops"], pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert "fetch" not in settings.mcp.servers


def test_time_server_with_timezone():
    runner = _make_runner(
        servers=["pyclawops", "time"],
        pyclawops_config=_make_pyclawops_config(timezone="Europe/London"),
    )
    settings = runner._build_fa_settings()
    args = settings.mcp.servers["time"].args
    assert "--local-timezone" in args
    assert "Europe/London" in args


def test_filesystem_server_defined_when_requested():
    runner = _make_runner(servers=["pyclawops", "filesystem"], pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert "filesystem" in settings.mcp.servers
    assert settings.mcp.servers["filesystem"].command == "npx"


# ── chrome-devtools ───────────────────────────────────────────────────────────

def test_chrome_devtools_added_when_enabled_and_requested():
    runner = _make_runner(
        servers=["pyclawops", "chrome-devtools"],
        pyclawops_config=_make_pyclawops_config(chrome_enabled=True, chrome_auto_connect=True),
    )
    settings = runner._build_fa_settings()
    assert "chrome-devtools" in settings.mcp.servers
    assert "--autoConnect" in settings.mcp.servers["chrome-devtools"].args
    assert settings.mcp.servers["chrome-devtools"].load_on_start is False


def test_chrome_devtools_skipped_when_not_enabled():
    runner = _make_runner(
        servers=["pyclawops", "chrome-devtools"],
        pyclawops_config=_make_pyclawops_config(chrome_enabled=False),
    )
    settings = runner._build_fa_settings()
    assert "chrome-devtools" not in settings.mcp.servers


def test_chrome_devtools_browser_url_in_args():
    runner = _make_runner(
        servers=["pyclawops", "chrome-devtools"],
        pyclawops_config=_make_pyclawops_config(
            chrome_enabled=True,
            chrome_browser_url="http://127.0.0.1:9222",
        ),
    )
    settings = runner._build_fa_settings()
    args = settings.mcp.servers["chrome-devtools"].args
    assert "--browserUrl" in args
    assert "http://127.0.0.1:9222" in args


# ── provider credentials ──────────────────────────────────────────────────────

def test_anthropic_key_in_settings():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(anthropic_key="sk-ant-abc"))
    settings = runner._build_fa_settings()
    assert settings.anthropic is not None
    assert settings.anthropic.api_key == "sk-ant-abc"


def test_openai_key_in_settings():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(openai_key="sk-oai-xyz"))
    settings = runner._build_fa_settings()
    assert settings.openai is not None
    assert settings.openai.api_key == "sk-oai-xyz"


def test_google_key_in_settings():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config(google_key="AIza-google"))
    settings = runner._build_fa_settings()
    assert settings.google is not None
    assert settings.google.api_key == "AIza-google"


def test_generic_provider_in_settings():
    runner = _make_runner(
        pyclawops_config=_make_pyclawops_config(generic_key="mm-key", generic_url="https://api.minimax.io"),
    )
    settings = runner._build_fa_settings()
    assert settings.generic is not None
    assert settings.generic.api_key == "mm-key"
    assert settings.generic.base_url == "https://api.minimax.io"


def test_no_providers_no_provider_blocks():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert settings.anthropic is None
    assert settings.openai is None
    assert settings.google is None
    assert settings.generic is None


def test_generic_fallback_from_api_key_field():
    """Without pyclawops_config, generic provider falls back to self.api_key/base_url."""
    runner = _make_runner(
        model="generic.MiniMax-M2.5",
        pyclawops_config=None,
        api_key="fallback-key",
        base_url="https://fallback.api/v1",
    )
    settings = runner._build_fa_settings()
    assert settings.generic is not None
    assert settings.generic.api_key == "fallback-key"
    assert settings.generic.base_url == "https://fallback.api/v1"


# ── logger always silenced ────────────────────────────────────────────────────

def test_logger_always_silent():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert settings.logger.progress_display is False
    assert settings.logger.show_chat is False
    assert settings.logger.show_tools is False
    assert settings.logger.streaming == "none"
    assert settings.logger.enable_markup is False


def test_default_model_is_passthrough():
    runner = _make_runner(pyclawops_config=_make_pyclawops_config())
    settings = runner._build_fa_settings()
    assert settings.default_model == "passthrough"


# ── unknown servers warn and are skipped ──────────────────────────────────────

def test_unknown_server_skipped(caplog):
    import logging
    runner = _make_runner(
        servers=["pyclawops", "my-unknown-server"],
        pyclawops_config=_make_pyclawops_config(),
    )
    with caplog.at_level(logging.WARNING, logger="pyclawops.agents.runner"):
        settings = runner._build_fa_settings()
    assert "my-unknown-server" not in settings.mcp.servers
    assert "my-unknown-server" in caplog.text
