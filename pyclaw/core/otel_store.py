"""In-process OpenTelemetry span store for the pyclaw TUI.

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
    from pyclaw.core import otel_store
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
    """Thread-safe ring buffer of finished ReadableSpan objects."""

    def __init__(self, maxlen: int = MAX_SPANS) -> None:
        self._spans: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, spans: Sequence) -> None:
        with self._lock:
            self._spans.extend(spans)

    def recent(self, n: int = 200) -> list:
        with self._lock:
            return list(self._spans)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)


# ── Custom exporter ───────────────────────────────────────────────────────────


class _StoreExporter:
    """Minimal SpanExporter that writes into a SpanStore."""

    def __init__(self, store: SpanStore) -> None:
        self._store = store

    def export(self, spans: Sequence) -> int:
        try:
            from opentelemetry.sdk.trace.export import SpanExportResult
            self._store.add(spans)
            return SpanExportResult.SUCCESS
        except Exception:
            return 1  # FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
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
            resource = Resource({SERVICE_NAME: "pyclaw"})
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
    """Return the active SpanStore, or None if bootstrap() was never called."""
    return _store


# ── Span helpers ──────────────────────────────────────────────────────────────


def span_summary(span) -> dict:
    """Extract a display-friendly dict from a ReadableSpan."""
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
