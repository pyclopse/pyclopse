"""Tests for FA config-level model settings (reasoning_effort, text_verbosity, service_tier).

Covers:
  - AgentConfig schema fields
  - AgentRunner stores and applies settings
  - agent.py wires settings from config to both base runner and session runners
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Schema ────────────────────────────────────────────────────────────────────

def test_agent_config_reasoning_effort_default():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig()
    assert cfg.reasoning_effort is None


def test_agent_config_reasoning_effort_set():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig.model_validate({"model": "sonnet", "reasoningEffort": "high"})
    assert cfg.reasoning_effort == "high"


def test_agent_config_text_verbosity_default():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig()
    assert cfg.text_verbosity is None


def test_agent_config_text_verbosity_set():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig.model_validate({"model": "sonnet", "textVerbosity": "low"})
    assert cfg.text_verbosity == "low"


def test_agent_config_service_tier_default():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig()
    assert cfg.service_tier is None


def test_agent_config_service_tier_set():
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig.model_validate({"model": "sonnet", "serviceTier": "flex"})
    assert cfg.service_tier == "flex"


def test_agent_config_all_fa_settings_snake_case():
    """snake_case aliases also accepted."""
    from pyclopse.config.schema import AgentConfig
    cfg = AgentConfig.model_validate({
        "model": "sonnet",
        "reasoning_effort": "medium",
        "text_verbosity": "high",
        "service_tier": "fast",
    })
    assert cfg.reasoning_effort == "medium"
    assert cfg.text_verbosity == "high"
    assert cfg.service_tier == "fast"


# ── AgentRunner stores settings ───────────────────────────────────────────────

def test_agent_runner_stores_reasoning_effort():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner.__init__(
        agent_name="test",
        instruction="hi",
        reasoning_effort="xhigh",
    )
    assert runner.reasoning_effort == "xhigh"


def test_agent_runner_stores_text_verbosity():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(
        agent_name="test",
        instruction="hi",
        text_verbosity="low",
    )
    assert runner.text_verbosity == "low"


def test_agent_runner_stores_service_tier():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(
        agent_name="test",
        instruction="hi",
        service_tier="flex",
    )
    assert runner.service_tier == "flex"


def test_agent_runner_defaults_are_none():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi")
    assert runner.reasoning_effort is None
    assert runner.text_verbosity is None
    assert runner.service_tier is None


# ── _apply_fa_model_settings ──────────────────────────────────────────────────

def test_apply_fa_model_settings_skips_when_all_none():
    """No FA calls when all settings are None."""
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi")
    mock_app = MagicMock()
    runner._app = mock_app

    runner._apply_fa_model_settings()

    mock_app._agent.assert_not_called()


def test_apply_fa_model_settings_reasoning_effort():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi", reasoning_effort="high")
    runner._log_prefix = "[test]"

    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.llm = mock_llm
    mock_app = MagicMock()
    mock_app._agent.return_value = mock_agent
    runner._app = mock_app

    mock_setting = MagicMock()
    with patch("fast_agent.llm.reasoning_effort.parse_reasoning_setting", return_value=mock_setting) as mock_parse:
        runner._apply_fa_model_settings()
        mock_parse.assert_called_once_with("high")
        mock_llm.set_reasoning_effort.assert_called_once_with(mock_setting)


def test_apply_fa_model_settings_text_verbosity():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi", text_verbosity="low")
    runner._log_prefix = "[test]"

    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.llm = mock_llm
    mock_app = MagicMock()
    mock_app._agent.return_value = mock_agent
    runner._app = mock_app

    runner._apply_fa_model_settings()
    mock_llm.set_text_verbosity.assert_called_once_with("low")


def test_apply_fa_model_settings_service_tier():
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi", service_tier="flex")
    runner._log_prefix = "[test]"

    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.llm = mock_llm
    mock_app = MagicMock()
    mock_app._agent.return_value = mock_agent
    runner._app = mock_app

    runner._apply_fa_model_settings()
    mock_llm.set_service_tier.assert_called_once_with("flex")


def test_apply_fa_model_settings_swallows_exceptions():
    """Exceptions do not propagate — runner still usable."""
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner(agent_name="test", instruction="hi", service_tier="flex")
    runner._log_prefix = "[test]"

    mock_agent = MagicMock()
    mock_agent.llm = None  # causes AttributeError when set_service_tier called
    mock_app = MagicMock()
    mock_app._agent.return_value = mock_agent
    runner._app = mock_app

    # Should not raise
    runner._apply_fa_model_settings()


# ── agent.py wiring ───────────────────────────────────────────────────────────

def test_agent_init_wires_fa_settings_to_base_runner():
    """Agent._init_fastagent() passes FA settings to AgentRunner."""
    from pyclopse.config.schema import AgentConfig
    from pyclopse.core.agent import Agent

    cfg = AgentConfig.model_validate({
        "model": "sonnet",
        "reasoningEffort": "medium",
        "textVerbosity": "high",
        "serviceTier": "fast",
        "useFastagent": True,
    })

    with patch("pyclopse.core.agent.FASTAGENT_AVAILABLE", True), \
         patch("pyclopse.core.agent.get_factory") as mock_factory, \
         patch("pyclopse.agents.runner.AgentRunner") as MockRunner:
        mock_factory.return_value.create_agent.return_value = MagicMock()
        mock_runner_inst = MagicMock()
        mock_runner_inst.model = "sonnet"
        mock_runner_inst.servers = ["pyclopse"]
        mock_runner_inst.tools_config = {}
        MockRunner.return_value = mock_runner_inst

        agent = Agent(id="a1", name="main", config=cfg)

        # Verify AgentRunner was called with the FA settings
        call_kwargs = MockRunner.call_args[1] if MockRunner.call_args else {}
        assert call_kwargs.get("reasoning_effort") == "medium"
        assert call_kwargs.get("text_verbosity") == "high"
        assert call_kwargs.get("service_tier") == "fast"


def test_get_session_runner_inherits_fa_settings():
    """_get_session_runner() forwards FA settings from base runner."""
    from pyclopse.config.schema import AgentConfig
    from pyclopse.core.agent import Agent

    cfg = AgentConfig.model_validate({
        "model": "sonnet",
        "useFastagent": True,
        "reasoningEffort": "low",
    })

    with patch("pyclopse.core.agent.FASTAGENT_AVAILABLE", True), \
         patch("pyclopse.core.agent.get_factory") as mock_factory, \
         patch("pyclopse.agents.runner.AgentRunner") as MockRunner:
        mock_factory.return_value.create_agent.return_value = MagicMock()
        # base runner stub
        base_runner = MagicMock()
        base_runner.model = "sonnet"
        base_runner.servers = ["pyclopse"]
        base_runner.tools_config = {}
        base_runner.show_thinking = False
        base_runner.api_key = None
        base_runner.base_url = None
        base_runner.request_params = {}
        base_runner.reasoning_effort = "low"
        base_runner.text_verbosity = None
        base_runner.service_tier = None
        base_runner.top_p = None
        base_runner.max_iterations = None
        base_runner.parallel_tool_calls = None
        base_runner.streaming_timeout = None
        MockRunner.return_value = base_runner

        agent = Agent(id="a1", name="main", config=cfg)
        agent.fast_agent_runner = base_runner

        MockRunner.reset_mock()
        session_runner = MagicMock()
        MockRunner.return_value = session_runner

        agent._get_session_runner("sess-123")

        call_kwargs = MockRunner.call_args[1] if MockRunner.call_args else {}
        assert call_kwargs.get("reasoning_effort") == "low"
