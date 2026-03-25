"""Tests for the model fallback chain in Agent._handle_with_fastagent().

Covers:
  - AgentConfig.fallbacks schema field
  - No fallback: error propagates as-is
  - Single fallback: tried on error, notice prepended
  - Multiple fallbacks: tried in order
  - _fallback_index persisted in session.context
  - Subsequent messages use active fallback model (index preserved)
  - Fallback chain exhausted: error re-raised
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Schema ────────────────────────────────────────────────────────────────────

def test_agent_config_fallbacks_default():
    from pyclaw.config.schema import AgentConfig
    cfg = AgentConfig()
    assert cfg.fallbacks == []


def test_agent_config_fallbacks_set():
    from pyclaw.config.schema import AgentConfig
    cfg = AgentConfig.model_validate({
        "model": "sonnet",
        "fallbacks": ["claude-haiku", "gpt-4o"],
    })
    assert cfg.fallbacks == ["claude-haiku", "gpt-4o"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_session(agent_id="main", fallback_index=0, model_override=None):
    """Return a minimal Session stub."""
    session = MagicMock()
    session.id = "sess-abc123"
    session.agent_id = agent_id
    session.history_path = None
    session.context = {}
    if fallback_index:
        session.context["_fallback_index"] = fallback_index
    if model_override:
        session.context["model_override"] = model_override
    return session


def _make_agent(fallbacks=None, model="primary-model"):
    """Return an Agent stub with configurable fallbacks and a mock runner factory."""
    from pyclaw.core.agent import Agent
    from pyclaw.config.schema import AgentConfig

    cfg = AgentConfig.model_validate({
        "model": model,
        "fallbacks": fallbacks or [],
    })

    agent = Agent.__new__(Agent)
    agent.id = "main"
    agent.name = "main"
    agent.config = cfg
    agent._session_runners = {}
    agent._tasks = []
    agent.is_running = True
    agent.current_session = None

    import logging
    object.__setattr__(agent, "_logger", logging.getLogger("test.agent"))

    # Base runner mock
    base_runner = MagicMock()
    base_runner.model = model
    base_runner.servers = ["pyclaw"]
    base_runner.tools_config = {}
    base_runner.show_thinking = False
    base_runner.api_key = None
    base_runner.base_url = None
    base_runner.request_params = {}
    base_runner.reasoning_effort = None
    base_runner.text_verbosity = None
    base_runner.service_tier = None
    base_runner.top_p = None
    base_runner.max_iterations = None
    base_runner.parallel_tool_calls = None
    base_runner.streaming_timeout = None
    agent.fast_agent_runner = base_runner

    return agent


# ── No fallbacks: error propagates ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_fallbacks_error_propagates():
    agent = _make_agent(fallbacks=[])
    session = _make_session()

    mock_runner = AsyncMock()
    mock_runner.run = AsyncMock(side_effect=RuntimeError("model unavailable"))

    with patch.object(agent, "_get_session_runner", return_value=mock_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="model unavailable"):
            await agent._handle_with_fastagent("hello", session)


# ── Single fallback ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_tried_on_primary_error():
    agent = _make_agent(fallbacks=["fallback-model"])
    session = _make_session()

    primary_runner = AsyncMock()
    primary_runner.run = AsyncMock(side_effect=RuntimeError("rate limit"))
    fallback_runner = AsyncMock()
    fallback_runner.run = AsyncMock(return_value="fallback response")

    runners = [primary_runner, fallback_runner]
    runner_idx = [0]

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        r = runners[runner_idx[0]]
        runner_idx[0] = min(runner_idx[0] + 1, len(runners) - 1)
        return r

    with patch.object(agent, "_get_session_runner", side_effect=get_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        result = await agent._handle_with_fastagent("hello", session)

    assert "fallback response" in result
    assert "↪️ Model Fallback" in result
    assert "fallback-model" in result


@pytest.mark.asyncio
async def test_fallback_notice_includes_tried_model():
    agent = _make_agent(model="primary-model", fallbacks=["backup-model"])
    session = _make_session()

    primary_runner = AsyncMock()
    primary_runner.run = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    fallback_runner = AsyncMock()
    fallback_runner.run = AsyncMock(return_value="backup reply")

    runners = [primary_runner, fallback_runner]
    runner_idx = [0]

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        r = runners[runner_idx[0]]
        runner_idx[0] = min(runner_idx[0] + 1, len(runners) - 1)
        return r

    with patch.object(agent, "_get_session_runner", side_effect=get_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        result = await agent._handle_with_fastagent("hello", session)

    assert "primary-model" in result
    assert "backup-model" in result


@pytest.mark.asyncio
async def test_fallback_index_saved_to_session_context():
    agent = _make_agent(fallbacks=["fallback-model"])
    session = _make_session()

    primary_runner = AsyncMock()
    primary_runner.run = AsyncMock(side_effect=RuntimeError("error"))
    fallback_runner = AsyncMock()
    fallback_runner.run = AsyncMock(return_value="ok")

    runners = [primary_runner, fallback_runner]
    runner_idx = [0]

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        r = runners[runner_idx[0]]
        runner_idx[0] = min(runner_idx[0] + 1, len(runners) - 1)
        return r

    with patch.object(agent, "_get_session_runner", side_effect=get_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        await agent._handle_with_fastagent("hello", session)

    assert session.context.get("_fallback_index") == 1


# ── Fallback chain exhausted ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_fallbacks_exhausted_raises():
    agent = _make_agent(fallbacks=["fb1", "fb2"])
    session = _make_session()

    def make_failing_runner():
        r = AsyncMock()
        r.run = AsyncMock(side_effect=RuntimeError("unavailable"))
        return r

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        return make_failing_runner()

    with patch.object(agent, "_get_session_runner", side_effect=get_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="unavailable"):
            await agent._handle_with_fastagent("hello", session)


# ── Active fallback used on subsequent messages ───────────────────────────────

@pytest.mark.asyncio
async def test_subsequent_messages_use_active_fallback():
    """Once _fallback_index=1, runner is created with model_override=fallback-model."""
    agent = _make_agent(model="primary", fallbacks=["fallback-model"])
    session = _make_session(fallback_index=1)

    captured_overrides = []

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        captured_overrides.append(model_override)
        r = AsyncMock()
        r.run = AsyncMock(return_value="fallback reply")
        return r

    with patch.object(agent, "_get_session_runner", side_effect=get_runner):
        result = await agent._handle_with_fastagent("hello", session)

    assert result == "fallback reply"
    assert captured_overrides[0] == "fallback-model"


# ── model_override + fallbacks ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_chain_uses_user_model_override_as_primary():
    """If model_override is set, it becomes the primary; fallbacks still apply."""
    agent = _make_agent(model="base-model", fallbacks=["fallback-model"])
    session = _make_session(model_override="overridden-model")

    overrides_seen = []

    primary_runner = AsyncMock()
    primary_runner.run = AsyncMock(side_effect=RuntimeError("overridden unavailable"))
    fallback_runner = AsyncMock()
    fallback_runner.run = AsyncMock(return_value="fallback ok")

    runner_map = {0: primary_runner, 1: fallback_runner}
    call_count = [0]

    def get_runner(session_id, model_override=None, history_path=None, instruction_override=None, priority="critical"):
        overrides_seen.append(model_override)
        r = runner_map[call_count[0]]
        call_count[0] += 1
        return r

    with patch.object(agent, "_get_session_runner", side_effect=get_runner), \
         patch.object(agent, "evict_session_runner", new_callable=AsyncMock):
        result = await agent._handle_with_fastagent("hello", session)

    # First call should use the user model override
    assert overrides_seen[0] == "overridden-model"
    # Second call uses the first fallback
    assert overrides_seen[1] == "fallback-model"
    assert "fallback ok" in result
