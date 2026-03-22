"""Tests for pyclaw/core/queue.py — per-session message queue modes."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from pyclaw.config.schema import DropPolicy, QueueConfig, QueueMode, QueueModeByChannel
from pyclaw.core.queue import QueueManager, SessionMessageQueue


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_queue(
    mode=QueueMode.COLLECT,
    debounce_ms=0,
    cap=20,
    drop=DropPolicy.OLD,
    dispatch_fn=None,
):
    if dispatch_fn is None:
        dispatch_fn = AsyncMock(return_value="response")
    return SessionMessageQueue(
        session_key="test:u1",
        mode=mode,
        debounce_ms=debounce_ms,
        cap=cap,
        drop=drop,
        dispatch_fn=dispatch_fn,
    ), dispatch_fn


# ─── schema ───────────────────────────────────────────────────────────────────

def test_queue_config_defaults():
    cfg = QueueConfig()
    assert cfg.mode == QueueMode.COLLECT
    assert cfg.debounce_ms == 300
    assert cfg.cap == 20
    assert cfg.drop == DropPolicy.OLD


def test_queue_config_camel_case():
    cfg = QueueConfig.model_validate({
        "mode": "interrupt",
        "debounceMs": 500,
        "cap": 10,
        "drop": "new",
    })
    assert cfg.mode == QueueMode.INTERRUPT
    assert cfg.debounce_ms == 500
    assert cfg.cap == 10
    assert cfg.drop == DropPolicy.NEW


def test_queue_config_snake_case_alias():
    cfg = QueueConfig.model_validate({
        "mode": "followup",
        "debounce_ms": 100,
    })
    assert cfg.mode == QueueMode.FOLLOWUP
    assert cfg.debounce_ms == 100


def test_agent_config_has_queue():
    from pyclaw.config.schema import AgentConfig
    cfg = AgentConfig()
    assert cfg.queue.mode == QueueMode.COLLECT
    assert cfg.queue.debounce_ms == 300


# ─── followup ─────────────────────────────────────────────────────────────────

async def test_followup_processes_in_order():
    order = []

    async def dispatch(content, **kw):
        order.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.FOLLOWUP, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")
    f2 = await q.enqueue("second")
    r1, r2 = await asyncio.gather(f1, f2)
    assert r1 == "first"
    assert r2 == "second"
    assert order == ["first", "second"]


async def test_followup_single_message():
    q, dispatch = _make_queue(mode=QueueMode.FOLLOWUP)
    dispatch.return_value = "ok"
    fut = await q.enqueue("hello")
    result = await fut
    assert result == "ok"
    dispatch.assert_called_once_with("hello")


# ─── collect ──────────────────────────────────────────────────────────────────

async def test_collect_batches_rapid_messages():
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.COLLECT, dispatch_fn=dispatch)
    f1 = await q.enqueue("hello")
    f2 = await q.enqueue("world")
    f3 = await q.enqueue("!")
    await asyncio.gather(f1, f2, f3)
    # All three dispatched in a single call
    assert len(calls) == 1
    assert "hello" in calls[0]
    assert "world" in calls[0]
    assert "!" in calls[0]


async def test_collect_single_message():
    q, dispatch = _make_queue(mode=QueueMode.COLLECT)
    dispatch.return_value = "single"
    fut = await q.enqueue("only")
    result = await fut
    assert result == "single"
    dispatch.assert_called_once_with("only")


# ─── interrupt ────────────────────────────────────────────────────────────────

async def test_interrupt_cancels_current():
    processing = asyncio.Event()
    released = asyncio.Event()
    results = []

    async def slow_dispatch(content, **kw):
        if not processing.is_set():
            processing.set()
            await released.wait()
        results.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.INTERRUPT, dispatch_fn=slow_dispatch)

    f1 = await q.enqueue("slow")
    await processing.wait()  # first message is in-flight

    f2 = await q.enqueue("interrupt")
    released.set()  # unblock slow dispatch (it's already cancelled, no-op)

    # f1 cancelled; f2 completes
    with pytest.raises((asyncio.CancelledError, Exception)):
        await f1

    result2 = await f2
    assert result2 == "interrupt"
    assert "interrupt" in results


async def test_interrupt_clears_queue():
    """Queued messages before the interrupt are cancelled."""
    waiting = asyncio.Event()
    calls = []

    async def dispatch(content, **kw):
        if not waiting.is_set():
            waiting.set()
            await asyncio.sleep(10)  # park indefinitely
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.INTERRUPT, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")
    await waiting.wait()
    f2 = await q.enqueue("queued_but_will_be_cleared")
    f3 = await q.enqueue("interrupt_wins")

    # f1 and f2 should be cancelled; f3 should win
    with pytest.raises((asyncio.CancelledError, Exception)):
        await asyncio.wait_for(f2, timeout=2.0)

    await asyncio.wait_for(f3, timeout=2.0)
    assert "interrupt_wins" in calls


# ─── steer ────────────────────────────────────────────────────────────────────

async def test_steer_combines_with_framing():
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(content, **kw):
        if not started.is_set():
            started.set()
            await release.wait()
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.STEER, dispatch_fn=dispatch)
    f1 = await q.enqueue("original question")
    await started.wait()

    f2 = await q.enqueue("actually, focus on X")
    release.set()

    results = await asyncio.gather(f1, f2, return_exceptions=True)
    # The combined dispatch call must contain the steer framing
    assert len(calls) >= 1
    combined = calls[-1]
    assert "original question" in combined
    assert "focus on X" in combined
    assert "follow-up" in combined.lower() or "User sent" in combined


async def test_steer_single_message_no_steer_framing():
    """With only one message, steer dispatches it plain."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.STEER, dispatch_fn=dispatch)
    fut = await q.enqueue("just one")
    result = await fut
    assert result == "just one"
    assert calls == ["just one"]


# ─── steer-backlog ────────────────────────────────────────────────────────────

async def test_steer_backlog_no_cancel():
    """steer-backlog never cancels; original message completes first."""
    order = []
    started = asyncio.Event()

    async def dispatch(content, **kw):
        if not started.is_set():
            started.set()
        order.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.STEER_BACKLOG, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")
    # Wait for drain loop to start processing f1
    await started.wait()
    f2 = await q.enqueue("follow-up")

    r1 = await f1
    r2 = await f2
    # first must complete before follow-up
    assert order[0] == "first"
    assert r1 == "first"
    # follow-up may be dispatched alone or combined
    assert r2 is not None


async def test_steer_backlog_combines_follow_ups():
    """When follow-ups arrive before drain fires, steer-backlog combines them all."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    # debounce_ms=0: drain yields once, allowing all three enqueues to land first
    q, _ = _make_queue(mode=QueueMode.STEER_BACKLOG, dispatch_fn=dispatch)
    f1 = await q.enqueue("original")
    f2 = await q.enqueue("correction A")
    f3 = await q.enqueue("correction B")

    await asyncio.gather(f1, f2, f3)
    # All three arrived before drain ran — combined into one steer-framed dispatch
    assert len(calls) == 1
    assert "original" in calls[0]
    assert "correction A" in calls[0]
    assert "correction B" in calls[0]


# ─── debounce ─────────────────────────────────────────────────────────────────

async def test_debounce_batches_burst():
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.COLLECT, debounce_ms=80, dispatch_fn=dispatch)
    # Enqueue 3 messages within the debounce window
    f1 = await q.enqueue("a")
    f2 = await q.enqueue("b")
    f3 = await q.enqueue("c")
    await asyncio.gather(f1, f2, f3)
    # Only 1 dispatch call with all three
    assert len(calls) == 1
    assert "a" in calls[0] and "b" in calls[0] and "c" in calls[0]


# ─── cap + drop ───────────────────────────────────────────────────────────────

async def test_cap_drop_new():
    """Drop policy=new silently drops the incoming message when at cap."""
    blocker = asyncio.Event()

    async def dispatch(content, **kw):
        await blocker.wait()
        return content

    q, _ = _make_queue(mode=QueueMode.FOLLOWUP, cap=1, drop=DropPolicy.NEW, dispatch_fn=dispatch)
    f1 = await q.enqueue("keep")   # will be dispatched
    f2 = await q.enqueue("keep2")  # queued (cap=1 not yet hit because queue is 0 after pop)
    f3 = await q.enqueue("drop")   # queue already has 1 item → drop=new → dropped

    # f3 should be immediately cancelled
    assert f3.cancelled() or f3.done()

    blocker.set()
    r1 = await f1
    assert r1 == "keep"


async def test_cap_drop_old():
    """Drop policy=old evicts the oldest message to make room."""
    blocker = asyncio.Event()
    started = asyncio.Event()

    async def dispatch(content, **kw):
        if not started.is_set():
            started.set()
            await blocker.wait()
        return content

    q, _ = _make_queue(mode=QueueMode.FOLLOWUP, cap=1, drop=DropPolicy.OLD, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")    # in-flight immediately
    await started.wait()
    f2 = await q.enqueue("second")   # queued (cap=1, queue=0 now — first is inflight)
    f3 = await q.enqueue("third")    # queue=1 → drop oldest (second)

    # f2 (second) should be cancelled
    blocker.set()
    r1 = await f1
    assert r1 == "first"

    with pytest.raises((asyncio.CancelledError, Exception)):
        await asyncio.wait_for(f2, timeout=1.0)


async def test_cap_drop_summarize_adds_label():
    """Drop policy=summarize prepends a [Multiple messages queued] label."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.COLLECT, cap=1, drop=DropPolicy.SUMMARIZE, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")
    f2 = await q.enqueue("second")   # evicts first with summarize label pending
    f3 = await q.enqueue("third")    # cap hit again

    await asyncio.gather(f1, f2, f3, return_exceptions=True)
    assert any("[Multiple messages queued]" in c for c in calls)


# ─── QueueManager ─────────────────────────────────────────────────────────────

async def test_queue_manager_get_or_create():
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    cfg = QueueConfig(mode=QueueMode.FOLLOWUP)
    q1 = mgr.get_or_create("s:1", cfg, dispatch)
    q2 = mgr.get_or_create("s:1", cfg, dispatch)
    assert q1 is q2  # same session key → same queue


async def test_queue_manager_update_config_live():
    dispatch = AsyncMock(return_value="ok")
    cfg = QueueConfig(mode=QueueMode.COLLECT)
    mgr = QueueManager()
    q = mgr.get_or_create("s:1", cfg, dispatch)
    assert q._mode == QueueMode.COLLECT

    result = mgr.update_config("s:1", mode="followup")
    assert result is True
    assert q._mode == QueueMode.FOLLOWUP


def test_queue_manager_update_nonexistent():
    mgr = QueueManager()
    result = mgr.update_config("nonexistent", mode="collect")
    assert result is False
    # Override stored for future queue creation
    assert mgr.get_config_override("nonexistent") == {"mode": "collect"}


async def test_queue_manager_override_applied_on_create():
    """Config overrides stored before queue creation are applied when queue is made."""
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    mgr.update_config("s:new", mode="interrupt")

    cfg = QueueConfig(mode=QueueMode.COLLECT)
    q = mgr.get_or_create("s:new", cfg, dispatch)
    assert q._mode == QueueMode.INTERRUPT  # override takes precedence


async def test_queue_manager_remove():
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    cfg = QueueConfig()
    mgr.get_or_create("s:1", cfg, dispatch)
    assert "s:1" in mgr._queues
    mgr.remove("s:1")
    assert "s:1" not in mgr._queues


# ─── queue mode ───────────────────────────────────────────────────────────────

async def test_queue_mode_processes_each_message_separately():
    """queue mode dispatches each message individually, in order."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.QUEUE, dispatch_fn=dispatch)
    f1 = await q.enqueue("first")
    f2 = await q.enqueue("second")
    f3 = await q.enqueue("third")
    r1, r2, r3 = await asyncio.gather(f1, f2, f3)
    assert r1 == "first"
    assert r2 == "second"
    assert r3 == "third"
    assert calls == ["first", "second", "third"]


async def test_queue_mode_no_combining():
    """queue mode never batches messages into a single dispatch."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.QUEUE, dispatch_fn=dispatch)
    f1 = await q.enqueue("a")
    f2 = await q.enqueue("b")
    await asyncio.gather(f1, f2)
    # Each message dispatched separately, never combined
    assert len(calls) == 2
    assert "a" in calls
    assert "b" in calls


async def test_queue_mode_no_cancellation():
    """queue mode never cancels in-flight messages."""
    processing = asyncio.Event()
    released = asyncio.Event()
    completed = []

    async def dispatch(content, **kw):
        if not processing.is_set():
            processing.set()
            await released.wait()
        completed.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.QUEUE, dispatch_fn=dispatch)
    f1 = await q.enqueue("slow")
    await processing.wait()
    f2 = await q.enqueue("next")
    released.set()

    r1, r2 = await asyncio.gather(f1, f2)
    assert r1 == "slow"   # original completed — not cancelled
    assert r2 == "next"
    assert completed == ["slow", "next"]


# ─── steer+backlog ────────────────────────────────────────────────────────────

async def test_steer_plus_backlog_cancels_and_combines():
    """steer+backlog cancels the current task and combines with steer framing."""
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(content, **kw):
        if not started.is_set():
            started.set()
            await release.wait()
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.STEER_PLUS_BACKLOG, dispatch_fn=dispatch)
    f1 = await q.enqueue("original question")
    await started.wait()

    f2 = await q.enqueue("actually, focus on X")
    release.set()

    results = await asyncio.gather(f1, f2, return_exceptions=True)
    assert len(calls) >= 1
    combined = calls[-1]
    assert "original question" in combined
    assert "focus on X" in combined
    assert "follow-up" in combined.lower() or "User sent" in combined


async def test_steer_plus_backlog_single_message():
    """With only one message, steer+backlog dispatches it plain."""
    calls = []

    async def dispatch(content, **kw):
        calls.append(content)
        return content

    q, _ = _make_queue(mode=QueueMode.STEER_PLUS_BACKLOG, dispatch_fn=dispatch)
    fut = await q.enqueue("solo message")
    result = await fut
    assert result == "solo message"
    assert calls == ["solo message"]


# ─── per-channel overrides ────────────────────────────────────────────────────

def test_queue_mode_by_channel_schema():
    """QueueModeByChannel parses correctly from camelCase."""
    from pyclaw.config.schema import QueueModeByChannel
    cfg = QueueConfig.model_validate({
        "mode": "collect",
        "byChannel": {
            "telegram": "interrupt",
            "slack": "followup",
        },
    })
    assert cfg.mode == QueueMode.COLLECT
    assert cfg.by_channel.telegram == QueueMode.INTERRUPT
    assert cfg.by_channel.slack == QueueMode.FOLLOWUP
    assert cfg.by_channel.discord is None


async def test_per_channel_override_applied_on_create():
    """Queue created for telegram:123 uses the telegram channel override."""
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    cfg = QueueConfig.model_validate({
        "mode": "collect",
        "byChannel": {"telegram": "interrupt"},
    })
    q = mgr.get_or_create("telegram:123", cfg, dispatch)
    assert q._mode == QueueMode.INTERRUPT


async def test_per_channel_override_not_applied_to_other_channel():
    """Slack queue is not affected by a telegram-only channel override."""
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    cfg = QueueConfig.model_validate({
        "mode": "collect",
        "byChannel": {"telegram": "interrupt"},
    })
    q = mgr.get_or_create("slack:T123", cfg, dispatch)
    assert q._mode == QueueMode.COLLECT


async def test_session_override_beats_channel_override():
    """A /queue session override takes precedence over the byChannel config."""
    dispatch = AsyncMock(return_value="ok")
    mgr = QueueManager()
    cfg = QueueConfig.model_validate({
        "mode": "collect",
        "byChannel": {"telegram": "interrupt"},
    })
    mgr.update_config("telegram:123", mode="followup")
    q = mgr.get_or_create("telegram:123", cfg, dispatch)
    assert q._mode == QueueMode.FOLLOWUP


# ─── update_config live ───────────────────────────────────────────────────────

def test_session_queue_update_config():
    dispatch = AsyncMock(return_value="ok")
    q = SessionMessageQueue("k", QueueMode.COLLECT, 300, 20, DropPolicy.OLD, dispatch)
    q.update_config(mode="interrupt", debounce_ms=0, cap=5, drop="new")
    assert q._mode == QueueMode.INTERRUPT
    assert q._debounce_ms == 0
    assert q._cap == 5
    assert q._drop == DropPolicy.NEW
