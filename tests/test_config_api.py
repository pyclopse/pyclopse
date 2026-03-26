"""
Tests for /api/v1/config HTTP endpoints and gateway.reload_config().
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Redaction helper tests
# ---------------------------------------------------------------------------

class TestRedaction:

    def test_api_key_redacted(self):
        from pyclopse.api.routes.config import _redact
        data = {"api_key": "secret123", "name": "openai"}
        result = _redact(data)
        assert result["api_key"] == "***REDACTED***"
        assert result["name"] == "openai"

    def test_bot_token_redacted(self):
        from pyclopse.api.routes.config import _redact
        data = {"botToken": "123:abc", "enabled": True}
        result = _redact(data)
        assert result["botToken"] == "***REDACTED***"
        assert result["enabled"] is True

    def test_nested_redaction(self):
        from pyclopse.api.routes.config import _redact
        data = {"providers": {"openai": {"api_key": "sk-xxx", "model": "gpt-4"}}}
        result = _redact(data)
        assert result["providers"]["openai"]["api_key"] == "***REDACTED***"
        assert result["providers"]["openai"]["model"] == "gpt-4"

    def test_list_not_broken(self):
        from pyclopse.api.routes.config import _redact
        data = {"tags": ["a", "b"], "api_key": "secret"}
        result = _redact(data)
        assert result["tags"] == ["a", "b"]
        assert result["api_key"] == "***REDACTED***"

    def test_non_sensitive_keys_unchanged(self):
        from pyclopse.api.routes.config import _redact
        data = {"host": "0.0.0.0", "port": 8080, "debug": False}
        assert _redact(data) == data


# ---------------------------------------------------------------------------
# GET /api/v1/config
# ---------------------------------------------------------------------------

def _make_app_with_gateway(gateway):
    from pyclopse.api.app import create_app, set_gateway
    app = create_app(gateway=gateway)
    set_gateway(gateway)
    return app


class TestGetConfig:

    def _make_gateway(self):
        from pyclopse.config.schema import Config
        gw = MagicMock()
        gw.config = Config.model_validate({"version": "1.0"})
        return gw

    @pytest.mark.asyncio
    async def test_returns_200(self):
        gw = self._make_gateway()
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/config/")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_response_has_config_key(self):
        gw = self._make_gateway()
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/config/")
        data = r.json()
        assert "config" in data

    @pytest.mark.asyncio
    async def test_sensitive_fields_redacted(self):
        from pyclopse.config.schema import Config, TelegramConfig, ChannelsConfig
        gw = MagicMock()
        gw.config = Config.model_validate({
            "version": "1.0",
            "channels": {
                "telegram": {"botToken": "real-secret-token", "enabled": True}
            }
        })
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/config/")
        # The token must not appear anywhere in the response text
        assert "real-secret-token" not in r.text
        assert "***REDACTED***" in r.text

    @pytest.mark.asyncio
    async def test_version_in_response(self):
        gw = self._make_gateway()
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/config/")
        assert r.json()["config"]["version"] == "1.0"


# ---------------------------------------------------------------------------
# POST /api/v1/config/reload
# ---------------------------------------------------------------------------

class TestReloadConfig:

    def _make_gateway_with_reload(self, changed=None):
        gw = MagicMock()
        gw.reload_config = AsyncMock(return_value=changed or {})
        return gw

    @pytest.mark.asyncio
    async def test_reload_returns_200(self):
        gw = self._make_gateway_with_reload()
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/config/reload")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_reload_calls_gateway_reload(self):
        gw = self._make_gateway_with_reload()
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/v1/config/reload")
        gw.reload_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_response_has_fields(self):
        gw = self._make_gateway_with_reload({"gateway.log_level": {"old": "info", "new": "debug"}})
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/config/reload")
        data = r.json()
        assert data["reloaded"] is True
        assert "changed" in data

    @pytest.mark.asyncio
    async def test_reload_error_returns_500(self):
        gw = MagicMock()
        gw.reload_config = AsyncMock(side_effect=RuntimeError("disk error"))
        app = _make_app_with_gateway(gw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/config/reload")
        assert r.status_code == 500


# ---------------------------------------------------------------------------
# gateway.reload_config() unit tests
# ---------------------------------------------------------------------------

class TestGatewayReloadConfig:

    def _make_gateway(self, tmp_path):
        """Real (lightweight) gateway with a temp config path."""
        from pyclopse.core.gateway import Gateway
        from pyclopse.config.loader import ConfigLoader
        gw = Gateway.__new__(Gateway)
        loader = MagicMock()
        from pyclopse.config.schema import Config
        loader.load.return_value = Config.model_validate({"version": "1.0"})
        gw._config_loader = loader
        gw._config = Config.model_validate({"version": "1.0"})
        gw._logger = MagicMock()
        return gw

    @pytest.mark.asyncio
    async def test_reload_returns_empty_when_no_changes(self, tmp_path):
        gw = self._make_gateway(tmp_path)
        changed = await gw.reload_config()
        assert changed == {}

    @pytest.mark.asyncio
    async def test_reload_detects_log_level_change(self, tmp_path):
        from pyclopse.config.schema import Config
        gw = self._make_gateway(tmp_path)
        gw._config = Config.model_validate({"version": "1.0", "gateway": {"log_level": "info"}})
        new_config = Config.model_validate({"version": "1.0", "gateway": {"log_level": "debug"}})
        gw._config_loader.load.return_value = new_config
        changed = await gw.reload_config()
        assert "gateway.log_level" in changed
        assert changed["gateway.log_level"]["new"] == "debug"

    @pytest.mark.asyncio
    async def test_reload_detects_concurrency_change(self, tmp_path):
        from pyclopse.config.schema import Config
        gw = self._make_gateway(tmp_path)
        gw._config = Config.model_validate({"version": "1.0", "concurrency": {"default": 3}})
        new_config = Config.model_validate({"version": "1.0", "concurrency": {"default": 5}})
        gw._config_loader.load.return_value = new_config
        with patch("pyclopse.core.concurrency.init_manager"):
            changed = await gw.reload_config()
        assert "concurrency" in changed

    @pytest.mark.asyncio
    async def test_reload_replaces_config(self, tmp_path):
        from pyclopse.config.schema import Config
        gw = self._make_gateway(tmp_path)
        gw._config = Config.model_validate({"version": "1.0"})
        new_config = Config.model_validate({"version": "2.0"})
        gw._config_loader.load.return_value = new_config
        await gw.reload_config()
        assert gw._config.version == "2.0"
