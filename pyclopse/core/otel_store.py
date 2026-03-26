"""In-process OpenTelemetry span store for the pyclopse TUI.

Strategy
--------
We bootstrap a real SDK TracerProvider with our custom exporter
**before** any AgentRunner initialises.  FastAgent acquires its tracers
lazily via ``trace.get_tracer(__name__)``; as long as the global provider
is ours by the time the first runner starts, all LLM + MCP tool spans
flow into our ring buffer automatically — no changes to _build_fa_settings
or fastagent.config.yaml required.

Usage
-----
    # In gateway.initialize(), before _init_core():
    from pyclopse.core import otel_store
    otel_store.bootstrap()

    # In TUI or any code:
    store = otel_store.get_store()
    if store:
        for span in store.recent(100):
            ...
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional, Sequence

import logging

logger = logging.getLogger(__name__)

# ── Public constants ──────────────────────────────────────────────────────────

MAX_SPANS = 1000  # Ring-buffer capacity

# ── Internal singleton ────────────────────────────────────────────────────────

_store: Optional["SpanStore"] = None
_lock = threading.Lock()


# ── SpanStore ─────────────────────────────────────────────────────────────────


class SpanStore:
    """Thread-safe ring buffer of finished OpenTelemetry ReadableSpan objects.

    Backed by a collections.deque with a fixed maximum length so old spans
    are automatically evicted when the buffer is full.

    Attributes:
        _spans (deque): The ring buffer holding span objects.
        _lock (threading.Lock): Mutex protecting all deque access.
    """

    def __init__(self, maxlen: int = MAX_SPANS) -> None:
        """Initialize the SpanStore.

        Args:
            maxlen (int): Maximum number of spans to retain. Oldest spans are
                evicted when the buffer is full. Defaults to MAX_SPANS (1000).
        """
        self._spans: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, spans: Sequence) -> None:
        """Append a batch of finished spans to the ring buffer.

        Args:
            spans (Sequence): Iterable of ReadableSpan objects to store.
        """
        with self._lock:
            self._spans.extend(spans)

    def recent(self, n: int = 200) -> list:
        """Return the most recent n spans.

        Args:
            n (int): Maximum number of spans to return. Defaults to 200.

        Returns:
            list: Up to n ReadableSpan objects, newest last.
        """
        with self._lock:
            return list(self._spans)[-n:]

    def clear(self) -> None:
        """Remove all spans from the buffer."""
        with self._lock:
            self._spans.clear()

    def __len__(self) -> int:
        """Return the number of spans currently in the buffer.

        Returns:
            int: Current span count.
        """
        with self._lock:
            return len(self._spans)


# ── Custom exporter ───────────────────────────────────────────────────────────


class _StoreExporter:
    """Minimal SpanExporter that writes finished spans into a SpanStore.

    Implements the opentelemetry-sdk SpanExporter interface so it can be
    registered with a TracerProvider via SimpleSpanProcessor.

    Attributes:
        _store (SpanStore): The target ring buffer.
    """

    def __init__(self, store: SpanStore) -> None:
        """Initialize the exporter.

        Args:
            store (SpanStore): Ring buffer to receive exported spans.
        """
        self._store = store

    def export(self, spans: Sequence) -> int:
        """Export a batch of finished spans to the store.

        Args:
            spans (Sequence): ReadableSpan objects from the SDK processor.

        Returns:
            int: SpanExportResult.SUCCESS (0) on success, 1 on failure.
        """
        try:
            from opentelemetry.sdk.trace.export import SpanExportResult
            self._store.add(spans)
            return SpanExportResult.SUCCESS
        except Exception:
            return 1  # FAILURE

    def shutdown(self) -> None:
        """Perform any cleanup on exporter shutdown.

        No-op for the in-process store exporter.
        """
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force-flush any buffered spans.

        Args:
            timeout_millis (int): Flush timeout in milliseconds. Ignored here.

        Returns:
            bool: Always True; the store writes synchronously.
        """
        return True


# ── Bootstrap ─────────────────────────────────────────────────────────────────


def bootstrap() -> "SpanStore":
    """Create a real SDK TracerProvider with our exporter and set it as global.

    Idempotent — safe to call multiple times.  Must be called before any
    AgentRunner.initialize() so that FastAgent's lazy trace.get_tracer()
    calls return a tracer backed by our provider.
    """
    global _store
    with _lock:
        if _store is not None:
            return _store

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME

            store = SpanStore()
            resource = Resource({SERVICE_NAME: "pyclopse"})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(SimpleSpanProcessor(_StoreExporter(store)))
            trace.set_tracer_provider(provider)
            _store = store
            logger.info("OTel span store bootstrapped (in-process, no external collector)")
            return _store
        except Exception as e:
            logger.warning(f"OTel bootstrap failed: {e}")
            # Return a dummy store so callers don't need to check for None
            _store = SpanStore()
            return _store


def get_store() -> Optional[SpanStore]:
    """Return the active SpanStore, or None if bootstrap() was never called.

    Returns:
        Optional[SpanStore]: The active store if bootstrapped, else None.
    """
    return _store


# ── Span helpers ──────────────────────────────────────────────────────────────


def span_summary(span) -> dict:
    """Extract a display-friendly dict from a ReadableSpan.

    Converts raw OTel span data into a flat dict suitable for rendering in
    the TUI Traces view.  All errors are caught and included as ``_err`` keys
    so the caller never needs to handle exceptions from span parsing.

    Args:
        span: An opentelemetry ReadableSpan object.

    Returns:
        dict: Keys include ``name``, ``ts`` (HH:MM:SS), ``dur`` (human
            duration string), ``dur_ms`` (float), ``in_toks``, ``out_toks``,
            ``model``, ``status``, ``attrs`` (dict), ``trace_id``,
            ``span_id``.  On error, also includes ``_err``.
    """
    try:
        from datetime import datetime, timezone

        start_ns = span.start_time or 0
        end_ns = span.end_time or start_ns
        dur_ms = max(0, (end_ns - start_ns) / 1_000_000)

        attrs = dict(span.attributes or {})
        status_code = "?"
        try:
            status_code = span.status.status_code.name
        except Exception:
            pass

        ts = datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).astimezone()

        if dur_ms >= 1000:
            dur_str = f"{dur_ms / 1000:.2f}s"
        elif dur_ms >= 1:
            dur_str = f"{dur_ms:.0f}ms"
        else:
            dur_str = "<1ms"

        in_toks = attrs.get("gen_ai.usage.input_tokens", "")
        out_toks = attrs.get("gen_ai.usage.output_tokens", "")
        model = attrs.get("gen_ai.request.model", "") or attrs.get("gen_ai.request.model", "")

        return {
            "name": span.name,
            "ts": ts.strftime("%H:%M:%S"),
            "dur": dur_str,
            "dur_ms": dur_ms,
            "in_toks": str(in_toks) if in_toks != "" else "—",
            "out_toks": str(out_toks) if out_toks != "" else "—",
            "model": model,
            "status": status_code,
            "attrs": attrs,
            "trace_id": f"{span.context.trace_id:032x}" if span.context else "",
            "span_id": f"{span.context.span_id:016x}" if span.context else "",
        }
    except Exception as e:
        return {"name": getattr(span, "name", "?"), "ts": "?", "dur": "?",
                "dur_ms": 0, "in_toks": "—", "out_toks": "—", "model": "",
                "status": "?", "attrs": {}, "trace_id": "", "span_id": "",
                "_err": str(e)}
