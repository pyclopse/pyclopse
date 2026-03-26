"""Tests for per-model concurrency manager."""
import asyncio
import time

import pytest

from pyclopse.core.concurrency import (
    ModelConcurrencyManager,
    get_manager,
    init_manager,
    _GLOBAL_DEFAULT,
)


# ---------------------------------------------------------------------------
# ModelConcurrencyManager unit tests
# ---------------------------------------------------------------------------

class TestModelConcurrencyManager:

    def test_default_limit(self):
        mgr = ModelConcurrencyManager()
        assert mgr.limit_for("unknown-model") == _GLOBAL_DEFAULT

    def test_explicit_model_limit(self):
        mgr = ModelConcurrencyManager(model_limits={"MiniMax-M2.5": 3, "gpt-4": 5})
        assert mgr.limit_for("MiniMax-M2.5") == 3
        assert mgr.limit_for("gpt-4") == 5

    def test_provider_prefix_stripped(self):
        """generic.MiniMax-M2.5 → matches MiniMax-M2.5 limit."""
        mgr = ModelConcurrencyManager(model_limits={"MiniMax-M2.5": 3})
        assert mgr.limit_for("generic.MiniMax-M2.5") == 3

    def test_custom_default(self):
        mgr = ModelConcurrencyManager(default=10)
        assert mgr.limit_for("any-model") == 10

    def test_configure_updates_limits(self):
        mgr = ModelConcurrencyManager(default=2)
        mgr.configure({"new-model": 7}, default=4)
        assert mgr.limit_for("new-model") == 7
        assert mgr.limit_for("other") == 4

    def test_semaphore_created_lazily(self):
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        assert "m" not in mgr._semaphores
        sem = mgr.semaphore_for("m")
        assert "m" in mgr._semaphores
        assert sem._value == 2

    def test_same_semaphore_returned(self):
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        assert mgr.semaphore_for("m") is mgr.semaphore_for("m")

    def test_status_empty(self):
        mgr = ModelConcurrencyManager()
        assert mgr.status() == {}

    @pytest.mark.asyncio
    async def test_acquire_releases(self):
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        sem = mgr.semaphore_for("m")
        assert sem._value == 2
        async with mgr.acquire("m"):
            assert sem._value == 1
        assert sem._value == 2

    @pytest.mark.asyncio
    async def test_status_shows_in_flight(self):
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        async with mgr.acquire("m"):
            status = mgr.status()
            assert status["m"]["in_flight"] == 1
            assert status["m"]["limit"] == 2
        # After release
        status = mgr.status()
        assert status["m"]["in_flight"] == 0

    @pytest.mark.asyncio
    async def test_concurrency_limit_enforced(self):
        """Only N tasks can hold the semaphore simultaneously."""
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        in_flight = 0
        max_in_flight = 0

        async def task():
            nonlocal in_flight, max_in_flight
            async with mgr.acquire("m"):
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(*[task() for _ in range(6)])
        assert max_in_flight <= 2

    @pytest.mark.asyncio
    async def test_all_tasks_complete(self):
        """All tasks eventually complete even when limit < task count."""
        mgr = ModelConcurrencyManager(model_limits={"m": 2})
        completed = []

        async def task(i):
            async with mgr.acquire("m"):
                await asyncio.sleep(0.01)
                completed.append(i)

        await asyncio.gather(*[task(i) for i in range(8)])
        assert sorted(completed) == list(range(8))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestGlobalManager:

    def test_get_manager_creates_singleton(self):
        mgr1 = get_manager()
        mgr2 = get_manager()
        assert mgr1 is mgr2

    def test_init_manager_replaces_singleton(self):
        mgr = init_manager({"x": 9}, default=5)
        assert get_manager() is mgr
        assert mgr.limit_for("x") == 9
        assert mgr.limit_for("other") == 5

    def test_init_manager_from_config(self):
        """Simulate what gateway._init_concurrency() does."""
        mgr = init_manager(
            model_limits={"MiniMax-M2.5": 3, "passthrough": 100},
            default=3,
        )
        assert mgr.limit_for("MiniMax-M2.5") == 3
        assert mgr.limit_for("passthrough") == 100
        assert mgr.limit_for("unknown") == 3
        # Provider-prefixed variant
        assert mgr.limit_for("generic.MiniMax-M2.5") == 3
