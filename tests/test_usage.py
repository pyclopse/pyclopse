"""Tests for pyclawops.core.usage — UsageMonitor, UsageRegistry, init_registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclawops.core.usage import (
    ThrottledError,
    UsageMonitor,
    UsageRegistry,
    _resolve_path,
    init_registry,
)


# ──────────────────────────────────────────────────────────────────────────────
# _resolve_path
# ──────────────────────────────────────────────────────────────────────────────

class TestResolvePath:
    def test_simple_key(self):
        assert _resolve_path({"used": 250}, "used") == 250

    def test_nested_key(self):
        data = {"billing": {"used": 100, "total": 1000}}
        assert _resolve_path(data, "billing.used") == 100

    def test_list_index(self):
        data = {"items": [{"count": 42}]}
        assert _resolve_path(data, "items.0.count") == 42

    def test_deep_list(self):
        data = {
            "model_remains": [
                {"current_interval_total_count": 4500,
                 "current_interval_usage_count": 354}
            ]
        }
        assert _resolve_path(data, "model_remains.0.current_interval_total_count") == 4500
        assert _resolve_path(data, "model_remains.0.current_interval_usage_count") == 354

    def test_missing_key_returns_none(self):
        assert _resolve_path({"a": 1}, "b") is None

    def test_missing_nested_returns_none(self):
        assert _resolve_path({"a": {"b": 1}}, "a.c") is None

    def test_out_of_bounds_index_returns_none(self):
        assert _resolve_path({"items": [1, 2]}, "items.5") is None

    def test_non_integer_index_returns_none(self):
        assert _resolve_path({"items": [1, 2]}, "items.x") is None

    def test_none_data_returns_none(self):
        assert _resolve_path(None, "anything") is None

    def test_root_list_index(self):
        assert _resolve_path([10, 20, 30], "1") == 20


# ──────────────────────────────────────────────────────────────────────────────
# UsageMonitor._parse_percent
# ──────────────────────────────────────────────────────────────────────────────

def _make_config(**kwargs):
    """Build a minimal UsageConfig-like mock."""
    cfg = MagicMock()
    cfg.endpoint = "https://example.com/usage"
    cfg.api_key = None
    cfg.params = {}
    cfg.check_interval = 300
    cfg.percent_path = kwargs.get("percent_path", None)
    cfg.total_path = kwargs.get("total_path", None)
    cfg.used_path = kwargs.get("used_path", None)
    cfg.remaining_path = kwargs.get("remaining_path", None)
    cfg.throttle = MagicMock()
    cfg.throttle.background = kwargs.get("background", 70)
    cfg.throttle.normal = kwargs.get("normal", 90)
    return cfg


class TestUsageMonitorParsePercent:
    def _monitor(self, **kwargs) -> UsageMonitor:
        return UsageMonitor("test", api_key=None, config=_make_config(**kwargs))

    def test_percent_path(self):
        m = self._monitor(percent_path="pct")
        assert m._parse_percent({"pct": 55.0}) == pytest.approx(55.0)

    def test_total_plus_remaining(self):
        m = self._monitor(total_path="total", remaining_path="remaining")
        result = m._parse_percent({"total": 1000, "remaining": 750})
        assert result == pytest.approx(25.0)  # (1000-750)/1000 * 100

    def test_total_plus_used(self):
        m = self._monitor(total_path="total", used_path="used")
        result = m._parse_percent({"total": 1000, "used": 400})
        assert result == pytest.approx(40.0)

    def test_minimax_format(self):
        """MiniMax response: total_path + remaining_path with nested list path."""
        m = self._monitor(
            total_path="model_remains.0.current_interval_total_count",
            remaining_path="model_remains.0.current_interval_usage_count",
        )
        data = {
            "model_remains": [{
                "current_interval_total_count": 4500,
                "current_interval_usage_count": 354,
            }]
        }
        # 354 remaining out of 4500 → (4500-354)/4500 * 100 ≈ 92.1%
        result = m._parse_percent(data)
        assert result == pytest.approx((4500 - 354) / 4500 * 100, rel=1e-3)

    def test_zero_total_skipped(self):
        m = self._monitor(total_path="total", used_path="used")
        assert m._parse_percent({"total": 0, "used": 0}) is None

    def test_missing_path_returns_none(self):
        m = self._monitor(percent_path="nonexistent")
        assert m._parse_percent({"something": 42}) is None

    def test_no_paths_configured_returns_none(self):
        m = self._monitor()
        assert m._parse_percent({"used": 100, "total": 1000}) is None

    def test_clamped_to_100(self):
        m = self._monitor(percent_path="pct")
        # store directly to test the clamping in the poll path
        # _parse_percent itself doesn't clamp — clamping is in _poll
        result = m._parse_percent({"pct": 150.0})
        assert result == 150.0  # raw value; _poll clamps


# ──────────────────────────────────────────────────────────────────────────────
# UsageMonitor.is_throttled
# ──────────────────────────────────────────────────────────────────────────────

class TestUsageMonitorThrottle:
    def _monitor(self, usage_pct: float, background: int = 70, normal: int = 90):
        m = UsageMonitor("test", api_key=None, config=_make_config(
            total_path="used", used_path="used",  # doesn't matter for these tests
            background=background, normal=normal,
        ))
        m._usage_pct = usage_pct
        return m

    def test_critical_never_throttled(self):
        m = self._monitor(99.0)
        assert m.is_throttled("critical") is False

    def test_background_throttled_at_threshold(self):
        m = self._monitor(70.0)
        assert m.is_throttled("background") is True

    def test_background_not_throttled_below(self):
        m = self._monitor(69.9)
        assert m.is_throttled("background") is False

    def test_normal_throttled_at_threshold(self):
        m = self._monitor(90.0)
        assert m.is_throttled("normal") is True

    def test_normal_not_throttled_below(self):
        m = self._monitor(89.9)
        assert m.is_throttled("normal") is False

    def test_background_not_throttled_by_normal_threshold(self):
        """background threshold is lower — so at 80% bg is throttled but at bg=70."""
        m = self._monitor(75.0, background=70, normal=90)
        assert m.is_throttled("background") is True
        assert m.is_throttled("normal") is False

    def test_no_usage_not_throttled(self):
        m = UsageMonitor("test", api_key=None, config=_make_config())
        m._usage_pct = None
        assert m.is_throttled("background") is False
        assert m.is_throttled("normal") is False


# ──────────────────────────────────────────────────────────────────────────────
# UsageRegistry
# ──────────────────────────────────────────────────────────────────────────────

class TestUsageRegistry:
    def _registry_with_monitor(self, usage_pct: float, background=70, normal=90):
        registry = UsageRegistry()
        monitor = UsageMonitor("prov", api_key=None, config=_make_config(
            background=background, normal=normal,
        ))
        monitor._usage_pct = usage_pct
        registry.register("prov", monitor, model_names=["ModelA", "ModelB"])
        return registry

    def test_check_critical_never_raises(self):
        reg = self._registry_with_monitor(99.0)
        reg.check("prov/ModelA", "critical")  # must not raise

    def test_check_background_throttled_raises(self):
        reg = self._registry_with_monitor(75.0, background=70)
        with pytest.raises(ThrottledError) as exc_info:
            reg.check("prov/ModelA", "background")
        err = exc_info.value
        assert err.provider == "prov"
        assert err.priority == "background"
        assert err.usage_pct == pytest.approx(75.0)
        assert err.threshold == 70

    def test_check_normal_throttled_raises(self):
        reg = self._registry_with_monitor(91.0, normal=90)
        with pytest.raises(ThrottledError):
            reg.check("ModelA", "normal")  # lookup by base model name

    def test_check_no_monitor_never_raises(self):
        reg = UsageRegistry()
        reg.check("unknown/model", "background")  # must not raise

    def test_lookup_by_provider_prefix(self):
        """Model string 'zai/glm-4.7' should hit the 'zai' monitor."""
        registry = UsageRegistry()
        monitor = UsageMonitor("zai", api_key=None, config=_make_config(background=70))
        monitor._usage_pct = 80.0
        registry.register("zai", monitor, model_names=["glm-4.7"])
        with pytest.raises(ThrottledError):
            registry.check("zai/glm-4.7", "background")

    def test_is_throttled_false_when_no_monitor(self):
        reg = UsageRegistry()
        assert reg.is_throttled("any/model", "background") is False

    def test_is_throttled_respects_priority(self):
        reg = self._registry_with_monitor(75.0, background=70, normal=90)
        assert reg.is_throttled("prov/ModelA", "background") is True
        assert reg.is_throttled("prov/ModelA", "normal") is False
        assert reg.is_throttled("prov/ModelA", "critical") is False

    def test_status_returns_all_monitors(self):
        reg = self._registry_with_monitor(55.0)
        status = reg.status()
        assert "prov" in status
        assert status["prov"]["usage_pct"] == pytest.approx(55.0)


# ──────────────────────────────────────────────────────────────────────────────
# ThrottledError
# ──────────────────────────────────────────────────────────────────────────────

class TestThrottledError:
    def test_message(self):
        err = ThrottledError("myprov", "background", 75.3, 70)
        assert "myprov" in str(err)
        assert "75.3" in str(err)
        assert "70" in str(err)
        assert "background" in str(err)

    def test_attributes(self):
        err = ThrottledError("p", "normal", 92.0, 90)
        assert err.provider == "p"
        assert err.priority == "normal"
        assert err.usage_pct == 92.0
        assert err.threshold == 90


# ──────────────────────────────────────────────────────────────────────────────
# init_registry
# ──────────────────────────────────────────────────────────────────────────────

class TestInitRegistry:
    def _make_usage_cfg(self, endpoint="https://example.com/usage"):
        from pyclawops.config.schema import UsageConfig
        return UsageConfig(
            enabled=True,
            endpoint=endpoint,
            total_path="total",
            used_path="used",
        )

    def test_creates_monitor_for_generic_provider_with_usage(self):
        from pyclawops.config.schema import GenericProviderConfig, ProvidersConfig
        usage_cfg = self._make_usage_cfg()
        pcfg = GenericProviderConfig(
            enabled=True,
            api_key="key123",
            fastagent_provider="generic",
            api_url="https://api.example.com",
            models={},
            usage=usage_cfg,
        )
        providers = ProvidersConfig()
        providers.model_extra["myprovider"] = pcfg

        reg = init_registry(providers)
        assert reg.get("myprovider") is not None

    def test_skips_provider_without_usage(self):
        from pyclawops.config.schema import GenericProviderConfig, ProvidersConfig
        pcfg = GenericProviderConfig(enabled=True, api_key="k", usage=None)
        providers = ProvidersConfig()
        providers.model_extra["nousage"] = pcfg

        reg = init_registry(providers)
        assert reg.get("nousage") is None

    def test_skips_disabled_usage(self):
        from pyclawops.config.schema import GenericProviderConfig, ProvidersConfig, UsageConfig
        usage_cfg = UsageConfig(enabled=False, endpoint="https://x.com/u")
        pcfg = GenericProviderConfig(enabled=True, api_key="k", usage=usage_cfg)
        providers = ProvidersConfig()
        providers.model_extra["disabled"] = pcfg

        reg = init_registry(providers)
        assert reg.get("disabled") is None

    def test_uses_provider_api_key_when_usage_key_omitted(self):
        from pyclawops.config.schema import GenericProviderConfig, ProvidersConfig
        usage_cfg = self._make_usage_cfg()
        assert usage_cfg.api_key is None
        pcfg = GenericProviderConfig(
            enabled=True,
            api_key="provider-key",
            usage=usage_cfg,
        )
        providers = ProvidersConfig()
        providers.model_extra["myprov"] = pcfg

        reg = init_registry(providers)
        monitor = reg.get("myprov")
        assert monitor is not None
        assert monitor._api_key == "provider-key"

    def test_usage_api_key_overrides_provider_key(self):
        from pyclawops.config.schema import GenericProviderConfig, ProvidersConfig, UsageConfig
        usage_cfg = UsageConfig(
            enabled=True,
            endpoint="https://x.com/u",
            api_key="usage-specific-key",
        )
        pcfg = GenericProviderConfig(
            enabled=True,
            api_key="provider-key",
            usage=usage_cfg,
        )
        providers = ProvidersConfig()
        providers.model_extra["myprov"] = pcfg

        reg = init_registry(providers)
        monitor = reg.get("myprov")
        assert monitor is not None
        assert monitor._api_key == "usage-specific-key"

    def test_empty_providers_returns_empty_registry(self):
        from pyclawops.config.schema import ProvidersConfig
        reg = init_registry(ProvidersConfig())
        assert reg.status() == {}


# ──────────────────────────────────────────────────────────────────────────────
# UsageConfig schema validation
# ──────────────────────────────────────────────────────────────────────────────

class TestUsageConfigSchema:
    def test_defaults(self):
        from pyclawops.config.schema import UsageConfig
        cfg = UsageConfig(endpoint="https://x.com/u")
        assert cfg.enabled is True
        assert cfg.check_interval == 300
        assert cfg.api_key is None
        assert cfg.params == {}
        assert cfg.throttle.background == 70
        assert cfg.throttle.normal == 90

    def test_camelcase_aliases(self):
        from pyclawops.config.schema import UsageConfig
        cfg = UsageConfig.model_validate({
            "endpoint": "https://x.com/u",
            "checkInterval": 120,
            "apiKey": "abc",
            "totalPath": "a.b",
            "usedPath": "c",
            "remainingPath": "d",
            "percentPath": "pct",
        })
        assert cfg.check_interval == 120
        assert cfg.api_key == "abc"
        assert cfg.total_path == "a.b"
        assert cfg.used_path == "c"
        assert cfg.remaining_path == "d"
        assert cfg.percent_path == "pct"

    def test_generic_provider_with_usage(self):
        from pyclawops.config.schema import GenericProviderConfig
        cfg = GenericProviderConfig.model_validate({
            "api_key": "k",
            "api_url": "https://x.com",
            "usage": {
                "endpoint": "https://x.com/u",
                "total_path": "total",
                "used_path": "used",
                "throttle": {"background": 60, "normal": 85},
            },
        })
        assert cfg.usage is not None
        assert cfg.usage.throttle.background == 60
        assert cfg.usage.throttle.normal == 85

    def test_generic_provider_without_usage_is_none(self):
        from pyclawops.config.schema import GenericProviderConfig
        cfg = GenericProviderConfig.model_validate({"api_key": "k"})
        assert cfg.usage is None


# ──────────────────────────────────────────────────────────────────────────────
# Job priority field
# ──────────────────────────────────────────────────────────────────────────────

class TestJobPriority:
    def test_default_priority_is_normal(self):
        from pyclawops.jobs.models import Job
        from datetime import datetime, timezone

        job_data = {
            "id": "test-id",
            "name": "my-job",
            "run": {"kind": "agent", "agent": "main", "message": "hello"},
            "schedule": {"kind": "interval", "seconds": 3600},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        job = Job.model_validate(job_data)
        assert job.priority == "normal"

    def test_custom_priority(self):
        from pyclawops.jobs.models import Job
        from datetime import datetime, timezone

        job_data = {
            "id": "test-id",
            "name": "bg-job",
            "priority": "background",
            "run": {"kind": "agent", "agent": "main", "message": "hello"},
            "schedule": {"kind": "interval", "seconds": 3600},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        job = Job.model_validate(job_data)
        assert job.priority == "background"
