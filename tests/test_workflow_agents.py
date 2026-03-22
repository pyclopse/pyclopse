"""Tests for orchestrator, iterative_planner, evaluator_optimizer, and maker workflows."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from pyclaw.config.schema import AgentConfig
from pyclaw.agents.runner import AgentRunner


# ── Schema tests ──────────────────────────────────────────────────────────────

class TestWorkflowSchemaFields:
    def test_orchestrator_fields_camelcase(self):
        cfg = AgentConfig.model_validate({
            "model": "sonnet",
            "workflow": "orchestrator",
            "agents": ["researcher", "writer"],
            "planType": "full",
            "planIterations": 5,
        })
        assert cfg.workflow == "orchestrator"
        assert cfg.agents == ["researcher", "writer"]
        assert cfg.plan_type == "full"
        assert cfg.plan_iterations == 5

    def test_iterative_planner_fields(self):
        cfg = AgentConfig.model_validate({
            "model": "sonnet",
            "workflow": "iterative_planner",
            "agents": ["coder", "reviewer"],
            "planIterations": -1,
        })
        assert cfg.workflow == "iterative_planner"
        assert cfg.plan_iterations == -1

    def test_evaluator_optimizer_fields_camelcase(self):
        cfg = AgentConfig.model_validate({
            "model": "sonnet",
            "workflow": "evaluator_optimizer",
            "generator": "drafter",
            "evaluator": "critic",
            "minRating": "EXCELLENT",
            "maxRefinements": 5,
            "refinementInstruction": "Improve clarity.",
        })
        assert cfg.generator == "drafter"
        assert cfg.evaluator == "critic"
        assert cfg.min_rating == "EXCELLENT"
        assert cfg.max_refinements == 5
        assert cfg.refinement_instruction == "Improve clarity."

    def test_maker_fields_camelcase(self):
        cfg = AgentConfig.model_validate({
            "model": "haiku",
            "workflow": "maker",
            "worker": "classifier",
            "k": 3,
            "maxSamples": 20,
            "matchStrategy": "normalized",
            "redFlagMaxLength": 100,
        })
        assert cfg.worker == "classifier"
        assert cfg.k == 3
        assert cfg.max_samples == 20
        assert cfg.match_strategy == "normalized"
        assert cfg.red_flag_max_length == 100

    def test_snake_case_aliases_also_work(self):
        cfg = AgentConfig.model_validate({
            "model": "sonnet",
            "plan_type": "iterative",
            "plan_iterations": 3,
            "min_rating": "GOOD",
            "max_refinements": 2,
            "max_samples": 10,
            "match_strategy": "exact",
            "red_flag_max_length": 50,
        })
        assert cfg.plan_type == "iterative"
        assert cfg.plan_iterations == 3
        assert cfg.min_rating == "GOOD"
        assert cfg.max_refinements == 2
        assert cfg.max_samples == 10
        assert cfg.match_strategy == "exact"
        assert cfg.red_flag_max_length == 50

    def test_workflow_fields_default_to_none(self):
        cfg = AgentConfig.model_validate({"model": "sonnet"})
        assert cfg.plan_type is None
        assert cfg.plan_iterations is None
        assert cfg.generator is None
        assert cfg.evaluator is None
        assert cfg.min_rating is None
        assert cfg.max_refinements is None
        assert cfg.refinement_instruction is None
        assert cfg.worker is None
        assert cfg.k is None
        assert cfg.max_samples is None
        assert cfg.match_strategy is None
        assert cfg.red_flag_max_length is None


# ── AgentRunner.__init__ tests ─────────────────────────────────────────────

class TestAgentRunnerWorkflowInit:
    def _make_runner(self, **kwargs):
        defaults = dict(agent_name="test", instruction="hi", model="sonnet")
        defaults.update(kwargs)
        return AgentRunner(**defaults)

    def test_no_workflow_by_default(self):
        r = self._make_runner()
        assert r.workflow is None
        assert r.child_agent_configs == {}

    def test_orchestrator_params_stored(self):
        children = {
            "researcher": {"instruction": "research", "model": "haiku", "servers": ["pyclaw"]},
            "writer": {"instruction": "write", "model": "sonnet", "servers": ["pyclaw"]},
        }
        r = self._make_runner(
            workflow="orchestrator",
            child_agent_configs=children,
            plan_type="full",
            plan_iterations=5,
        )
        assert r.workflow == "orchestrator"
        assert r.child_agent_configs == children
        assert r.plan_type == "full"
        assert r.plan_iterations == 5

    def test_evaluator_optimizer_params_stored(self):
        r = self._make_runner(
            workflow="evaluator_optimizer",
            child_agent_configs={
                "drafter": {"instruction": "draft", "model": "sonnet", "servers": []},
                "critic": {"instruction": "critique", "model": "sonnet", "servers": []},
            },
            generator="drafter",
            evaluator="critic",
            min_rating="EXCELLENT",
            max_refinements=4,
            refinement_instruction="Improve it.",
        )
        assert r.generator == "drafter"
        assert r.evaluator == "critic"
        assert r.min_rating == "EXCELLENT"
        assert r.max_refinements == 4
        assert r.refinement_instruction == "Improve it."

    def test_maker_params_stored(self):
        r = self._make_runner(
            workflow="maker",
            child_agent_configs={
                "classifier": {"instruction": "classify", "model": "haiku", "servers": []},
            },
            worker="classifier",
            k=3,
            max_samples=20,
            match_strategy="normalized",
            red_flag_max_length=100,
        )
        assert r.worker == "classifier"
        assert r.k == 3
        assert r.max_samples == 20
        assert r.match_strategy == "normalized"
        assert r.red_flag_max_length == 100


# ── _all_servers tests ─────────────────────────────────────────────────────

class TestAllServers:
    def _make_runner(self, servers, child_agent_configs=None):
        r = AgentRunner.__new__(AgentRunner)
        r.servers = servers
        r.child_agent_configs = child_agent_configs or {}
        return r

    def test_no_children_returns_parent_servers(self):
        r = self._make_runner(["pyclaw", "fetch"])
        assert r._all_servers() == ["pyclaw", "fetch"]

    def test_merges_child_servers(self):
        r = self._make_runner(
            ["pyclaw"],
            {
                "researcher": {"servers": ["pyclaw", "fetch"]},
                "writer": {"servers": ["pyclaw", "filesystem"]},
            },
        )
        result = r._all_servers()
        assert "pyclaw" in result
        assert "fetch" in result
        assert "filesystem" in result

    def test_deduplicates(self):
        r = self._make_runner(
            ["pyclaw", "fetch"],
            {"child": {"servers": ["fetch", "pyclaw"]}},
        )
        result = r._all_servers()
        assert result.count("pyclaw") == 1
        assert result.count("fetch") == 1

    def test_child_with_no_servers_key(self):
        r = self._make_runner(["pyclaw"], {"child": {"instruction": "hi"}})
        assert r._all_servers() == ["pyclaw"]


# ── _register_workflow tests ──────────────────────────────────────────────

def _make_runner_for_register(workflow, child_agent_configs=None, **kwargs):
    """Build a minimal AgentRunner stub for _register_workflow tests."""
    r = AgentRunner.__new__(AgentRunner)
    r.agent_name = "test_workflow"
    r.instruction = "You are an orchestrator."
    r.model = "sonnet"
    r.workflow = workflow
    r.child_agent_configs = child_agent_configs or {}
    r.plan_type = kwargs.get("plan_type", "full")
    r.plan_iterations = kwargs.get("plan_iterations", None)
    r.generator = kwargs.get("generator", None)
    r.evaluator = kwargs.get("evaluator", None)
    r.min_rating = kwargs.get("min_rating", "GOOD")
    r.max_refinements = kwargs.get("max_refinements", 3)
    r.refinement_instruction = kwargs.get("refinement_instruction", None)
    r.worker = kwargs.get("worker", None)
    r.k = kwargs.get("k", 3)
    r.max_samples = kwargs.get("max_samples", 50)
    r.match_strategy = kwargs.get("match_strategy", "exact")
    r.red_flag_max_length = kwargs.get("red_flag_max_length", None)
    r._log_prefix = "[test_workflow]"
    return r


class TestRegisterWorkflow:
    def _make_fa_mock(self):
        fast = MagicMock()
        fast.agent = MagicMock(return_value=lambda fn: fn)
        fast.orchestrator = MagicMock(return_value=lambda fn: fn)
        fast.iterative_planner = MagicMock(return_value=lambda fn: fn)
        fast.evaluator_optimizer = MagicMock(return_value=lambda fn: fn)
        fast.maker = MagicMock(return_value=lambda fn: fn)
        return fast

    def _make_fa_rp(self):
        from fast_agent.llm.request_params import RequestParams
        return RequestParams(maxTokens=4096)

    def _make_fa_settings(self, server_names):
        settings = MagicMock()
        settings.mcp.servers = {n: MagicMock() for n in server_names}
        return settings

    def test_orchestrator_registers_children_and_workflow(self):
        r = _make_runner_for_register(
            "orchestrator",
            child_agent_configs={
                "researcher": {"instruction": "research", "model": "haiku", "servers": ["pyclaw"]},
                "writer": {"instruction": "write", "model": "sonnet", "servers": ["pyclaw"]},
            },
            plan_type="full",
            plan_iterations=5,
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings(["pyclaw"])

        r._register_workflow(fast, rp, settings)

        # Child agents registered
        assert fast.agent.call_count == 2
        child_names = {c.kwargs["name"] for c in fast.agent.call_args_list}
        assert child_names == {"researcher", "writer"}

        # Orchestrator registered with default=True
        fast.orchestrator.assert_called_once()
        orch_kwargs = fast.orchestrator.call_args.kwargs
        assert orch_kwargs["name"] == "test_workflow"
        assert set(orch_kwargs["agents"]) == {"researcher", "writer"}
        assert orch_kwargs["plan_type"] == "full"
        assert orch_kwargs["plan_iterations"] == 5
        assert orch_kwargs["default"] is True

    def test_iterative_planner_registers_correctly(self):
        r = _make_runner_for_register(
            "iterative_planner",
            child_agent_configs={
                "coder": {"instruction": "code", "model": "sonnet", "servers": ["pyclaw"]},
            },
            plan_iterations=-1,
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings(["pyclaw"])

        r._register_workflow(fast, rp, settings)

        fast.iterative_planner.assert_called_once()
        kwargs = fast.iterative_planner.call_args.kwargs
        assert kwargs["plan_iterations"] == -1
        assert kwargs["default"] is True

    def test_evaluator_optimizer_registers_correctly(self):
        r = _make_runner_for_register(
            "evaluator_optimizer",
            child_agent_configs={
                "drafter": {"instruction": "draft", "model": "sonnet", "servers": []},
                "critic": {"instruction": "critique", "model": "sonnet", "servers": []},
            },
            generator="drafter",
            evaluator="critic",
            min_rating="EXCELLENT",
            max_refinements=4,
            refinement_instruction="Be better.",
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings(["pyclaw"])

        r._register_workflow(fast, rp, settings)

        fast.evaluator_optimizer.assert_called_once()
        kwargs = fast.evaluator_optimizer.call_args.kwargs
        assert kwargs["generator"] == "drafter"
        assert kwargs["evaluator"] == "critic"
        assert kwargs["min_rating"] == "EXCELLENT"
        assert kwargs["max_refinements"] == 4
        assert kwargs["refinement_instruction"] == "Be better."
        assert kwargs["default"] is True

    def test_maker_registers_correctly(self):
        r = _make_runner_for_register(
            "maker",
            child_agent_configs={
                "classifier": {"instruction": "classify", "model": "haiku", "servers": []},
            },
            worker="classifier",
            k=3,
            max_samples=15,
            match_strategy="normalized",
            red_flag_max_length=50,
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings(["pyclaw"])

        r._register_workflow(fast, rp, settings)

        fast.maker.assert_called_once()
        kwargs = fast.maker.call_args.kwargs
        assert kwargs["worker"] == "classifier"
        assert kwargs["k"] == 3
        assert kwargs["max_samples"] == 15
        assert kwargs["match_strategy"] == "normalized"
        assert kwargs["red_flag_max_length"] == 50
        assert kwargs["default"] is True

    def test_evaluator_optimizer_raises_without_generator(self):
        r = _make_runner_for_register(
            "evaluator_optimizer",
            child_agent_configs={"critic": {"instruction": "x", "model": "sonnet", "servers": []}},
            generator=None,
            evaluator="critic",
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings([])
        with pytest.raises(ValueError, match="generator"):
            r._register_workflow(fast, rp, settings)

    def test_maker_raises_without_worker(self):
        r = _make_runner_for_register(
            "maker",
            child_agent_configs={"x": {"instruction": "x", "model": "sonnet", "servers": []}},
            worker=None,
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings([])
        with pytest.raises(ValueError, match="worker"):
            r._register_workflow(fast, rp, settings)

    def test_unknown_workflow_raises(self):
        r = _make_runner_for_register(
            "unknown_type",
            child_agent_configs={"x": {"instruction": "x", "model": "s", "servers": []}},
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings([])
        with pytest.raises(ValueError, match="Unknown workflow"):
            r._register_workflow(fast, rp, settings)

    def test_child_server_filtered_to_available(self):
        """Children requesting unknown servers should silently get empty server list."""
        r = _make_runner_for_register(
            "orchestrator",
            child_agent_configs={
                "agent_a": {"instruction": "x", "model": "sonnet", "servers": ["pyclaw", "unknown_server"]},
            },
        )
        fast = self._make_fa_mock()
        rp = self._make_fa_rp()
        settings = self._make_fa_settings(["pyclaw"])  # unknown_server not present

        r._register_workflow(fast, rp, settings)

        agent_call = fast.agent.call_args_list[0]
        assert "unknown_server" not in agent_call.kwargs["servers"]
        assert "pyclaw" in agent_call.kwargs["servers"]


# ── History skipped for workflow runners ──────────────────────────────────

class TestWorkflowHistorySkipped:
    def _make_runner(self, workflow=None):
        r = AgentRunner.__new__(AgentRunner)
        r.workflow = workflow
        r._history_loaded = False
        r.history_path = MagicMock()
        r.history_path.exists.return_value = True
        r._app = MagicMock()
        return r

    @pytest.mark.asyncio
    async def test_load_history_skipped_for_workflow(self):
        r = self._make_runner(workflow="orchestrator")
        await r._load_history()
        # _history_loaded set to True but no FA calls made
        assert r._history_loaded is True
        r._app._agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_history_skipped_for_workflow(self):
        r = self._make_runner(workflow="evaluator_optimizer")
        await r._save_history()
        r._app._agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_history_runs_for_non_workflow(self):
        r = self._make_runner(workflow=None)
        r.history_path.exists.return_value = False  # nothing to load
        await r._load_history()
        # No FA call (file didn't exist) but it wasn't blocked by workflow guard
        assert r._history_loaded is True


# ── _translate_to_fa_model tests (agent.py helper) ────────────────────────

class TestTranslateToFaModel:
    def test_no_slash_returns_unchanged(self):
        from pyclaw.core.agent import _translate_to_fa_model
        assert _translate_to_fa_model("sonnet", None) == "sonnet"

    def test_strips_fastagent_prefix(self):
        from pyclaw.core.agent import _translate_to_fa_model
        assert _translate_to_fa_model("fastagent:sonnet", None) == "sonnet"
        assert _translate_to_fa_model("fa:sonnet", None) == "sonnet"

    def test_unknown_provider_returns_raw(self):
        from pyclaw.core.agent import _translate_to_fa_model
        cfg = MagicMock()
        cfg.providers = MagicMock()
        cfg.providers.unknown_provider = None
        result = _translate_to_fa_model("unknown_provider/model", cfg)
        assert result == "unknown_provider/model"

    def test_known_provider_with_fastagent_provider(self):
        import os
        from pyclaw.core.agent import _translate_to_fa_model
        cfg = MagicMock()
        provider = MagicMock()
        provider.fastagent_provider = "generic"
        provider.api_key = "test-key"
        provider.api_url = "http://localhost:8000"
        cfg.providers.minimax = provider
        result = _translate_to_fa_model("minimax/MiniMax-M2.5", cfg)
        assert result == "generic.MiniMax-M2.5"
        assert os.environ.get("GENERIC_API_KEY") == "test-key"

    def test_no_pyclaw_config_returns_raw(self):
        from pyclaw.core.agent import _translate_to_fa_model
        assert _translate_to_fa_model("openai/gpt-4o", None) == "openai/gpt-4o"
