"""Per-session message queue with configurable processing modes.

Modes
-----
followup        Process each message in order, one at a time. Respects
                debounce_ms and cap/drop.
collect         Batch all pending messages into a single combined dispatch.
interrupt       Cancel current processing; only handle the newest message.
steer           Cancel current processing; combine original + corrections
                into a steer-framed prompt.
steer-backlog   Never cancel; combine accumulated follow-ups with steer
                framing after the current dispatch finishes.
steer+backlog   Cancel current processing; combine all inflight + queued
                messages with steer framing. Equivalent to steer in
                behaviour; supported for OpenClaw config compatibility.
queue           Strict FIFO: process each message one at a time without
                cancellation or combining. Cap/drop still apply; debounce
                is skipped (messages drain immediately).

Config is defined in ``pyclaw.config.schema`` (QueueMode, DropPolicy, QueueConfig).
This module only imports from schema — no circular gateway imports.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pyclaw.config.schema import DropPolicy, QueueConfig, QueueMode

logger = logging.getLogger(__name__)


@dataclass
class QueuedMessage:
    """A single inbound message waiting for dispatch."""
    content: str
    kwargs: dict
    future: asyncio.Future
    arrived_at: float = field(default_factory=time.monotonic)


class SessionMessageQueue:
    """Per-session queue with a drain loop that applies the configured mode."""

    def __init__(
        self,
        session_key: str,
        mode: QueueMode,
        debounce_ms: int,
        cap: int,
        drop: DropPolicy,
        dispatch_fn: Callable,  # async (content: str, **kwargs) -> Optional[str]
    ):
        self.session_key = session_key
        self._mode = mode
        self._debounce_ms = debounce_ms
        self._cap = cap
        self._drop = drop
        self._dispatch_fn = dispatch_fn

        self._queue: List[QueuedMessage] = []
        self._drain_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        # Items currently being dispatched — preserved for steer re-insertion
        self._steer_inflight_items: List[QueuedMessage] = []
        self._summarize_label_pending: bool = False
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(f"pyclaw.queue.{session_key}")

    async def enqueue(self, content: str, **kwargs) -> asyncio.Future:
        """Add a message to the queue and return a Future that resolves with the response."""
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        msg = QueuedMessage(content=content, kwargs=kwargs, future=fut)

        async with self._lock:
            if self._mode == QueueMode.INTERRUPT:
                # Cancel current processing and all queued messages
                if self._current_task and not self._current_task.done():
                    self._current_task.cancel()
                for queued in self._queue:
                    if not queued.future.done():
                        queued.future.cancel()
                self._queue.clear()
                for item in self._steer_inflight_items:
                    if not item.future.done():
                        item.future.cancel()
                self._steer_inflight_items = []
                self._queue.append(msg)

            elif self._mode in (QueueMode.STEER, QueueMode.STEER_PLUS_BACKLOG):
                # Cancel current; keep existing queue so original gets re-inserted
                if self._current_task and not self._current_task.done():
                    self._current_task.cancel()
                self._queue.append(msg)

            elif self._mode == QueueMode.STEER_BACKLOG:
                # Never cancel; just accumulate
                self._queue.append(msg)

            else:  # followup | collect | queue
                self._apply_cap(msg)

            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(self._drain_loop())

        return fut

    def _apply_cap(self, msg: QueuedMessage) -> None:
        """Apply cap + drop policy for followup/collect modes."""
        if len(self._queue) < self._cap:
            self._queue.append(msg)
            return
        if self._drop == DropPolicy.NEW:
            msg.future.cancel()
            return
        # OLD and SUMMARIZE both evict the oldest message
        oldest = self._queue.pop(0)
        if not oldest.future.done():
            oldest.future.cancel()
        if self._drop == DropPolicy.SUMMARIZE:
            self._summarize_label_pending = True
        self._queue.append(msg)

    async def _drain_loop(self) -> None:
        """Drain loop — runs as a background task, processes queue per mode."""
        while True:
            # Debounce: let rapid bursts settle before draining.
            # queue mode skips debounce — it drains immediately one-by-one.
            if self._debounce_ms > 0 and self._mode != QueueMode.QUEUE:
                await asyncio.sleep(self._debounce_ms / 1000.0)
            else:
                await asyncio.sleep(0)  # yield once to allow concurrent enqueues

            async with self._lock:
                if not self._queue:
                    break

                mode = self._mode
                items: List[QueuedMessage]

                if mode in (QueueMode.FOLLOWUP, QueueMode.QUEUE):
                    items = [self._queue.pop(0)]
                    combined = items[0].content
                    kwargs = items[0].kwargs

                elif mode == QueueMode.COLLECT:
                    items = list(self._queue)
                    self._queue.clear()
                    contents = [m.content for m in items]
                    if self._summarize_label_pending:
                        contents.insert(0, "[Multiple messages queued]")
                        self._summarize_label_pending = False
                    combined = "\n".join(contents)
                    kwargs = items[-1].kwargs

                elif mode == QueueMode.INTERRUPT:
                    items = [self._queue.pop(0)]
                    combined = items[0].content
                    kwargs = items[0].kwargs

                elif mode in (QueueMode.STEER, QueueMode.STEER_PLUS_BACKLOG, QueueMode.STEER_BACKLOG):
                    items = list(self._queue)
                    self._queue.clear()
                    if len(items) >= 2:
                        original = items[0].content
                        corrections = "\n".join(m.content for m in items[1:])
                        combined = (
                            f"{original}\n\n"
                            f"[User sent follow-up while you were responding: {corrections}]"
                        )
                    else:
                        combined = items[0].content
                    kwargs = items[-1].kwargs

                else:
                    items = [self._queue.pop(0)]
                    combined = items[0].content
                    kwargs = items[0].kwargs

                self._steer_inflight_items = list(items)
                futures = [m.future for m in items]
                self._current_task = asyncio.create_task(
                    self._dispatch_fn(combined, **kwargs)
                )

            # Await the dispatch task OUTSIDE the lock to avoid deadlocks
            try:
                result = await self._current_task
                for fut in futures:
                    if not fut.done():
                        fut.set_result(result)

            except asyncio.CancelledError:
                if self._mode in (QueueMode.STEER, QueueMode.STEER_PLUS_BACKLOG, QueueMode.STEER_BACKLOG):
                    # Re-insert inflight items at front of queue; their futures remain
                    # pending and will be resolved when the steer-combined dispatch runs.
                    async with self._lock:
                        self._queue[:0] = list(self._steer_inflight_items)
                    # _steer_inflight_items cleared by finally below
                    continue
                else:
                    # interrupt: cancel any surviving futures
                    for fut in futures:
                        if not fut.done():
                            fut.cancel()
                    continue  # new message likely queued; loop to process it

            except Exception as exc:
                for fut in futures:
                    if not fut.done():
                        fut.set_exception(exc)

            finally:
                self._current_task = None
                self._steer_inflight_items = []

            async with self._lock:
                if not self._queue:
                    break

    def update_config(self, **kwargs) -> None:
        """Update live queue config — used by /queue command."""
        if "mode" in kwargs:
            self._mode = QueueMode(kwargs["mode"])
        if "debounce_ms" in kwargs:
            self._debounce_ms = int(kwargs["debounce_ms"])
        if "cap" in kwargs:
            self._cap = int(kwargs["cap"])
        if "drop" in kwargs:
            self._drop = DropPolicy(kwargs["drop"])


class QueueManager:
    """Manages per-session message queues across the gateway."""

    def __init__(self) -> None:
        self._queues: Dict[str, SessionMessageQueue] = {}
        # Session-level config overrides (set by /queue command)
        self._config_overrides: Dict[str, dict] = {}

    def get_or_create(
        self,
        session_key: str,
        base_config: QueueConfig,
        dispatch_fn: Callable,
    ) -> SessionMessageQueue:
        """Get existing queue or create one with the given config.

        Resolution order (highest to lowest priority):
          1. Per-session override (set by ``/queue`` command)
          2. Per-channel override (``queue.byChannel.<channel>`` in agent config)
          3. Agent base config (``queue.mode``)
        """
        if session_key not in self._queues:
            overrides = self._config_overrides.get(session_key, {})

            # Determine base mode: check per-channel override first
            channel = session_key.split(":")[0] if ":" in session_key else None
            channel_mode = (
                getattr(base_config.by_channel, channel, None)
                if channel and base_config.by_channel
                else None
            )
            default_mode = channel_mode or base_config.mode

            mode = QueueMode(overrides["mode"]) if "mode" in overrides else default_mode
            debounce_ms = overrides.get("debounce_ms", base_config.debounce_ms)
            cap = overrides.get("cap", base_config.cap)
            drop = DropPolicy(overrides["drop"]) if "drop" in overrides else base_config.drop
            self._queues[session_key] = SessionMessageQueue(
                session_key=session_key,
                mode=mode,
                debounce_ms=debounce_ms,
                cap=cap,
                drop=drop,
                dispatch_fn=dispatch_fn,
            )
        return self._queues[session_key]

    def remove(self, session_key: str) -> None:
        """Remove and cancel a session's queue."""
        q = self._queues.pop(session_key, None)
        if q and q._drain_task and not q._drain_task.done():
            q._drain_task.cancel()

    def update_config(self, session_key: str, **kwargs) -> bool:
        """Update config for a session key (stores override + applies to live queue).

        Returns True if a live queue was updated, False if only the override was stored.
        """
        if session_key not in self._config_overrides:
            self._config_overrides[session_key] = {}
        self._config_overrides[session_key].update(kwargs)
        q = self._queues.get(session_key)
        if q is None:
            return False
        q.update_config(**kwargs)
        return True

    def get_config_override(self, session_key: str) -> dict:
        """Return any stored config overrides for a session key."""
        return dict(self._config_overrides.get(session_key, {}))
