"""Tests for GET /api/v1/hooks."""

from unittest.mock import MagicMock
from fastapi.testclient import TestClient


def _make_gateway(hooks: dict | None = None):
    from pyclawops.core.gateway import Gateway
    gw = Gateway.__new__(Gateway)
    gw._initialized = True
    gw._is_running = True
    gw._logger = MagicMock()

    if hooks is not None:
        registry = MagicMock()
        registry.list_hooks.return_value = hooks
        registry.event_count.return_value = len(hooks)
        registry.handler_count.return_value = sum(len(v) for v in hooks.values())
        gw._hook_registry = registry
    else:
        gw._hook_registry = None

    return gw


def _make_app(gateway):
    from pyclawops.api.app import create_app, set_gateway
    set_gateway(gateway)
    return create_app(gateway)


class TestHooksEndpoint:

    def test_returns_200(self):
        gw = _make_gateway(hooks={})
        client = TestClient(_make_app(gw))
        resp = client.get("/api/v1/hooks/")
        assert resp.status_code == 200

    def test_empty_registry_returns_zeros(self):
        gw = _make_gateway(hooks={})
        client = TestClient(_make_app(gw))
        data = client.get("/api/v1/hooks/").json()
        assert data["total_events"] == 0
        assert data["total_handlers"] == 0
        assert data["events"] == {}

    def test_no_registry_returns_empty(self):
        gw = _make_gateway(hooks=None)
        client = TestClient(_make_app(gw))
        data = client.get("/api/v1/hooks/").json()
        assert data["total_events"] == 0
        assert data["events"] == {}

    def test_returns_events_and_handlers(self):
        hooks = {
            "gateway:startup": [
                {"name": "hook:boot-md", "priority": 0, "description": "boot", "source": "file:/x"}
            ],
            "command:reset": [
                {"name": "hook:session-memory", "priority": 0, "description": "save", "source": "file:/y"}
            ],
        }
        gw = _make_gateway(hooks=hooks)
        client = TestClient(_make_app(gw))
        data = client.get("/api/v1/hooks/").json()
        assert data["total_events"] == 2
        assert data["total_handlers"] == 2
        assert "gateway:startup" in data["events"]
        assert data["events"]["gateway:startup"][0]["name"] == "hook:boot-md"

    def test_total_handlers_sums_across_events(self):
        hooks = {
            "message:received": [{"name": "h1", "priority": 0, "description": "", "source": "code"},
                                  {"name": "h2", "priority": 1, "description": "", "source": "code"}],
            "message:sent":     [{"name": "h3", "priority": 0, "description": "", "source": "code"}],
        }
        gw = _make_gateway(hooks=hooks)
        client = TestClient(_make_app(gw))
        data = client.get("/api/v1/hooks/").json()
        assert data["total_handlers"] == 3
