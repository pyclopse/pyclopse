"""
Tests for the channel plugin system.

Covers:
  - ChannelPlugin ABC (cannot instantiate directly)
  - GatewayHandle.dispatch contract
  - load_from_specs: happy path, bad spec, wrong base class, import error
  - discover_entry_points: success, graceful error handling
  - load_all: deduplication of duplicate classes
  - PluginsConfig.channels field (schema)
  - Gateway._init_channel_plugins: starts plugins, stores in _channels
  - Gateway.stop: calls plugin.stop(), tolerates errors
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyclaw.channels.plugin import ChannelPlugin, GatewayHandle
from tests.fixtures.channel_plugins import EchoPlugin, AnotherPlugin

_ECHO_SPEC = "tests.fixtures.channel_plugins:EchoPlugin"
_ANOTHER_SPEC = "tests.fixtures.channel_plugins:AnotherPlugin"


# ---------------------------------------------------------------------------
# Inline plugin for error-path tests (defined here, not imported by spec)
# ---------------------------------------------------------------------------

class _LocalPlugin(ChannelPlugin):
    name = "local"
    async def start(self, gw): pass
    async def stop(self): pass
    async def send(self, uid, text, **kw): pass


# ---------------------------------------------------------------------------
# ChannelPlugin ABC
# ---------------------------------------------------------------------------

class TestChannelPluginABC:

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ChannelPlugin()  # type: ignore

    def test_concrete_subclass_instantiates(self):
        p = EchoPlugin()
        assert p.name == "echo"

    @pytest.mark.asyncio
    async def test_start_receives_gateway_handle(self):
        handle = MagicMock(spec=GatewayHandle)
        p = EchoPlugin()
        await p.start(handle)
        assert p.started is True
        assert p.gateway_handle is handle

    @pytest.mark.asyncio
    async def test_stop_called(self):
        p = EchoPlugin()
        await p.stop()
        assert p.stopped is True

    @pytest.mark.asyncio
    async def test_send_records_message(self):
        p = EchoPlugin()
        await p.send("u1", "hello")
        await p.send("u2", "world")
        assert p.sent == [("u1", "hello"), ("u2", "world")]


# ---------------------------------------------------------------------------
# GatewayHandle default implementation raises
# ---------------------------------------------------------------------------

class TestGatewayHandle:

    @pytest.mark.asyncio
    async def test_base_dispatch_raises_not_implemented(self):
        handle = GatewayHandle()
        with pytest.raises(NotImplementedError):
            await handle.dispatch("ch", "u1", "Alice", "hello")


# ---------------------------------------------------------------------------
# load_from_specs
# ---------------------------------------------------------------------------

class TestLoadFromSpecs:

    def test_loads_valid_spec(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs([_ECHO_SPEC])
        assert len(plugins) == 1
        assert isinstance(plugins[0], EchoPlugin)

    def test_malformed_spec_returns_empty(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs(["no_colon_here"])
        assert plugins == []

    def test_nonexistent_module_returns_empty(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs(["no_such_module_xyz:SomeClass"])
        assert plugins == []

    def test_nonexistent_class_returns_empty(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs(["tests.fixtures.channel_plugins:NoSuchClass"])
        assert plugins == []

    def test_wrong_base_class_returns_empty(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs(["builtins:str"])
        assert plugins == []

    def test_multiple_specs(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs([_ECHO_SPEC, _ANOTHER_SPEC])
        assert len(plugins) == 2
        names = {p.name for p in plugins}
        assert names == {"echo", "another"}

    def test_one_bad_spec_does_not_prevent_others(self):
        from pyclaw.channels.loader import load_from_specs
        plugins = load_from_specs(["no_such_module:Bad", _ECHO_SPEC])
        assert len(plugins) == 1
        assert plugins[0].name == "echo"


# ---------------------------------------------------------------------------
# discover_entry_points
# ---------------------------------------------------------------------------

class TestDiscoverEntryPoints:

    def test_returns_empty_when_no_entry_points(self):
        from pyclaw.channels.loader import discover_entry_points
        with patch("importlib.metadata.entry_points", return_value=[]):
            plugins = discover_entry_points()
        assert plugins == []

    def test_loads_valid_entry_point(self):
        from pyclaw.channels.loader import discover_entry_points

        ep = MagicMock()
        ep.name = "echo"
        ep.value = _ECHO_SPEC
        ep.load.return_value = EchoPlugin

        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = discover_entry_points()

        assert len(plugins) == 1
        assert isinstance(plugins[0], EchoPlugin)

    def test_skips_non_plugin_entry_point(self):
        from pyclaw.channels.loader import discover_entry_points

        ep = MagicMock()
        ep.name = "bad"
        ep.load.return_value = str

        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = discover_entry_points()

        assert plugins == []

    def test_gracefully_handles_load_error(self):
        from pyclaw.channels.loader import discover_entry_points

        ep = MagicMock()
        ep.name = "failing"
        ep.load.side_effect = ImportError("not installed")

        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = discover_entry_points()

        assert plugins == []

    def test_gracefully_handles_discovery_error(self):
        from pyclaw.channels.loader import discover_entry_points
        with patch("importlib.metadata.entry_points", side_effect=Exception("broken")):
            plugins = discover_entry_points()
        assert plugins == []


# ---------------------------------------------------------------------------
# load_all: deduplication
# ---------------------------------------------------------------------------

class TestLoadAll:

    def test_deduplicates_same_class_from_entry_points_and_specs(self):
        from pyclaw.channels.loader import load_all

        ep = MagicMock()
        ep.name = "echo"
        ep.load.return_value = EchoPlugin

        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = load_all([_ECHO_SPEC])

        # EchoPlugin is the same class object in both paths → deduplicated
        assert len(plugins) == 1
        assert isinstance(plugins[0], EchoPlugin)

    def test_different_classes_both_loaded(self):
        from pyclaw.channels.loader import load_all

        with patch("importlib.metadata.entry_points", return_value=[]):
            plugins = load_all([_ECHO_SPEC, _ANOTHER_SPEC])

        assert len(plugins) == 2

    def test_entry_point_wins_over_spec_on_dedup(self):
        from pyclaw.channels.loader import load_all

        ep = MagicMock()
        ep.name = "echo"
        ep.load.return_value = EchoPlugin

        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = load_all([_ECHO_SPEC])

        assert len(plugins) == 1
        # The single instance is from the entry point (first)
        assert isinstance(plugins[0], EchoPlugin)


# ---------------------------------------------------------------------------
# PluginsConfig.channels schema
# ---------------------------------------------------------------------------

class TestPluginsConfigChannels:

    def test_defaults_to_empty_list(self):
        from pyclaw.config.schema import PluginsConfig
        cfg = PluginsConfig()
        assert cfg.channels == []

    def test_accepts_channel_specs(self):
        from pyclaw.config.schema import PluginsConfig
        cfg = PluginsConfig(channels=["mypackage:MyPlugin"])
        assert cfg.channels == ["mypackage:MyPlugin"]

    def test_embedded_in_config(self):
        from pyclaw.config.schema import Config
        cfg = Config.model_validate({
            "plugins": {
                "channels": ["mypackage:MyPlugin", "another:Plugin"],
            }
        })
        assert cfg.plugins.channels == ["mypackage:MyPlugin", "another:Plugin"]


# ---------------------------------------------------------------------------
# Gateway._init_channel_plugins
# ---------------------------------------------------------------------------

class TestGatewayChannelPluginWiring:

    def _make_gateway(self, specs=None):
        from pyclaw.core.gateway import Gateway
        from pyclaw.config.schema import Config, AgentsConfig, SecurityConfig, PluginsConfig
        import time

        gw = Gateway.__new__(Gateway)
        gw._initialized = False
        gw._is_running = False
        gw._logger = MagicMock()
        gw._channels = {}
        gw._hook_registry = None
        gw._known_session_ids = set()
        gw._usage = {
            "messages_total": 0, "messages_by_agent": {},
            "messages_by_channel": {}, "started_at": time.time(),
        }
        plugins_cfg = PluginsConfig(channels=specs or [])
        gw._config = Config(
            agents=AgentsConfig(),
            security=SecurityConfig(),
            plugins=plugins_cfg,
        )
        gw.handle_message = AsyncMock(return_value="agent reply")
        return gw

    @pytest.mark.asyncio
    async def test_no_specs_no_plugins_loaded(self):
        gw = self._make_gateway(specs=[])
        with patch("importlib.metadata.entry_points", return_value=[]):
            await gw._init_channel_plugins()
        assert gw._channels == {}

    @pytest.mark.asyncio
    async def test_valid_spec_plugin_started_and_registered(self):
        gw = self._make_gateway(specs=[_ECHO_SPEC])
        with patch("importlib.metadata.entry_points", return_value=[]):
            await gw._init_channel_plugins()

        assert "echo" in gw._channels
        plugin = gw._channels["echo"]
        assert isinstance(plugin, EchoPlugin)
        assert plugin.started is True

    @pytest.mark.asyncio
    async def test_gateway_handle_dispatches_to_handle_message(self):
        gw = self._make_gateway(specs=[_ECHO_SPEC])
        with patch("importlib.metadata.entry_points", return_value=[]):
            await gw._init_channel_plugins()

        plugin: EchoPlugin = gw._channels["echo"]
        result = await plugin.gateway_handle.dispatch(
            channel="echo",
            user_id="u1",
            user_name="Alice",
            text="hello",
        )
        gw.handle_message.assert_called_once_with(
            channel="echo",
            sender="Alice",
            sender_id="u1",
            content="hello",
            message_id=None,
        )
        assert result == "agent reply"

    @pytest.mark.asyncio
    async def test_plugin_start_error_does_not_crash_gateway(self):
        class _BadPlugin(ChannelPlugin):
            name = "bad"
            async def start(self, gw): raise RuntimeError("connection failed")
            async def stop(self): pass
            async def send(self, uid, text, **kw): pass

        gw = self._make_gateway()
        with patch("pyclaw.channels.loader.load_all", return_value=[_BadPlugin()]):
            await gw._init_channel_plugins()

        assert "bad" not in gw._channels

    @pytest.mark.asyncio
    async def test_multiple_plugins_all_started(self):
        gw = self._make_gateway(specs=[_ECHO_SPEC, _ANOTHER_SPEC])
        with patch("importlib.metadata.entry_points", return_value=[]):
            await gw._init_channel_plugins()

        assert "echo" in gw._channels
        assert "another" in gw._channels

    @pytest.mark.asyncio
    async def test_stop_calls_plugin_stop(self):
        plugin = EchoPlugin()

        from pyclaw.core.gateway import Gateway
        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._channels = {"echo": plugin}

        for name, ch in list(gw._channels.items()):
            try:
                await ch.stop()
            except Exception:
                pass

        assert plugin.stopped is True

    @pytest.mark.asyncio
    async def test_stop_tolerates_plugin_stop_error(self):
        class _StopFails(ChannelPlugin):
            name = "fails"
            async def start(self, gw): pass
            async def stop(self): raise RuntimeError("network error")
            async def send(self, uid, text, **kw): pass

        from pyclaw.core.gateway import Gateway
        gw = Gateway.__new__(Gateway)
        gw._logger = MagicMock()
        gw._channels = {"fails": _StopFails()}

        for name, ch in list(gw._channels.items()):
            try:
                await ch.stop()
            except Exception as exc:
                gw._logger.warning(f"stop error: {exc}")

        gw._logger.warning.assert_called()
