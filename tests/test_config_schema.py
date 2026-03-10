"""
Tests for new config schema fields added in task #13.
Verifies defaults, parsing, and backward-compatibility.
"""

import pytest
from pyclaw.config.schema import (
    Config,
    AgentConfig,
    SessionsConfig,
    TelegramConfig,
    SlackConfig,
    WhatsAppConfig,
    SecurityConfig,
)


# ---------------------------------------------------------------------------
# SessionsConfig
# ---------------------------------------------------------------------------

class TestSessionsConfig:

    def test_defaults(self):
        cfg = SessionsConfig()
        assert cfg.persist_dir == "~/.pyclaw/sessions"
        assert cfg.ttl_hours == 24
        assert cfg.reaper_interval_minutes == 60

    def test_custom_values(self):
        cfg = SessionsConfig(persist_dir="/tmp/sess", ttl_hours=48, reaper_interval_minutes=30)
        assert cfg.persist_dir == "/tmp/sess"
        assert cfg.ttl_hours == 48
        assert cfg.reaper_interval_minutes == 30

    def test_camel_case_aliases(self):
        cfg = SessionsConfig.model_validate({
            "persistDir": "/tmp/x",
            "ttlHours": 12,
            "reaperIntervalMinutes": 15,
        })
        assert cfg.persist_dir == "/tmp/x"
        assert cfg.ttl_hours == 12
        assert cfg.reaper_interval_minutes == 15

    def test_root_config_has_sessions(self):
        cfg = Config()
        assert isinstance(cfg.sessions, SessionsConfig)
        assert cfg.sessions.ttl_hours == 24


# ---------------------------------------------------------------------------
# AgentConfig new fields
# ---------------------------------------------------------------------------

class TestAgentConfigNewFields:

    def test_show_thinking_defaults_false(self):
        cfg = AgentConfig()
        assert cfg.show_thinking is False

    def test_show_thinking_can_be_set(self):
        cfg = AgentConfig(show_thinking=True)
        assert cfg.show_thinking is True

    def test_show_thinking_camel_alias(self):
        cfg = AgentConfig.model_validate({"showThinking": True})
        assert cfg.show_thinking is True

    def test_typing_mode_defaults_none(self):
        cfg = AgentConfig()
        assert cfg.typing_mode == "none"

    def test_typing_mode_can_be_set(self):
        cfg = AgentConfig(typing_mode="typing")
        assert cfg.typing_mode == "typing"

    def test_typing_mode_camel_alias(self):
        cfg = AgentConfig.model_validate({"typingMode": "typing"})
        assert cfg.typing_mode == "typing"

    def test_existing_fields_unaffected(self):
        cfg = AgentConfig(name="TestBot", model="gpt-4", temperature=0.5)
        assert cfg.name == "TestBot"
        assert cfg.model == "gpt-4"
        assert cfg.temperature == 0.5


# ---------------------------------------------------------------------------
# TelegramConfig new fields
# ---------------------------------------------------------------------------

class TestTelegramConfigNewFields:

    def test_denied_users_defaults_empty(self):
        cfg = TelegramConfig()
        assert cfg.denied_users == []

    def test_topics_defaults_empty(self):
        cfg = TelegramConfig()
        assert cfg.topics == {}

    def test_typing_indicator_defaults_true(self):
        cfg = TelegramConfig()
        assert cfg.typing_indicator is True

    def test_typing_indicator_can_disable(self):
        cfg = TelegramConfig.model_validate({"typingIndicator": False})
        assert cfg.typing_indicator is False

    def test_topics_can_be_set(self):
        cfg = TelegramConfig.model_validate({
            "topics": {"general": 0, "alerts": 42}
        })
        assert cfg.topics["alerts"] == 42

    def test_denied_users_camel_alias(self):
        cfg = TelegramConfig.model_validate({"deniedUsers": [111, 222]})
        assert 111 in cfg.denied_users

    def test_existing_fields_unaffected(self):
        cfg = TelegramConfig.model_validate({"enabled": True, "allowedUsers": [12345]})
        assert cfg.enabled is True
        assert 12345 in cfg.allowed_users


# ---------------------------------------------------------------------------
# SlackConfig new fields
# ---------------------------------------------------------------------------

class TestSlackConfigNewFields:

    def test_threading_defaults_true(self):
        cfg = SlackConfig()
        assert cfg.threading is True

    def test_threading_can_disable(self):
        cfg = SlackConfig(threading=False)
        assert cfg.threading is False

    def test_allowed_users_defaults_empty(self):
        cfg = SlackConfig()
        assert cfg.allowed_users == []

    def test_denied_users_defaults_empty(self):
        cfg = SlackConfig()
        assert cfg.denied_users == []

    def test_camel_case_aliases(self):
        cfg = SlackConfig.model_validate({
            "allowedUsers": ["U123"],
            "deniedUsers": ["U456"],
        })
        assert "U123" in cfg.allowed_users
        assert "U456" in cfg.denied_users


# ---------------------------------------------------------------------------
# WhatsAppConfig new fields
# ---------------------------------------------------------------------------

class TestWhatsAppConfigNewFields:

    def test_allowed_users_defaults_empty(self):
        cfg = WhatsAppConfig()
        assert cfg.allowed_users == []

    def test_denied_users_defaults_empty(self):
        cfg = WhatsAppConfig()
        assert cfg.denied_users == []


# ---------------------------------------------------------------------------
# SecurityConfig new field
# ---------------------------------------------------------------------------

class TestSecurityConfigNewFields:

    def test_denied_users_defaults_empty(self):
        cfg = SecurityConfig()
        assert cfg.denied_users == []

    def test_denied_users_can_be_set(self):
        cfg = SecurityConfig.model_validate({"deniedUsers": [999]})
        assert 999 in cfg.denied_users

    def test_existing_fields_unaffected(self):
        cfg = SecurityConfig()
        assert cfg.audit.enabled is True
        assert cfg.sandbox.type == "none"


# ---------------------------------------------------------------------------
# Backward compatibility — existing configs (no new fields) still load
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:

    def test_minimal_config_loads(self):
        """Config with only version field should not crash."""
        cfg = Config.model_validate({"version": "1.0"})
        assert cfg.sessions.ttl_hours == 24

    def test_config_without_sessions_section(self):
        cfg = Config.model_validate({
            "version": "1.0",
            "agents": {},
        })
        assert isinstance(cfg.sessions, SessionsConfig)

    def test_telegram_without_new_fields(self):
        cfg = TelegramConfig.model_validate({"botToken": "abc123"})
        assert cfg.topics == {}
        assert cfg.typing_indicator is True

    def test_slack_without_new_fields(self):
        cfg = SlackConfig.model_validate({})
        assert cfg.threading is True
