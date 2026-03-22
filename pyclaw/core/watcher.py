"""File watcher for pyclaw — polls files for mtime changes and fires async callbacks.

Uses asyncio polling (no external dependencies).  Detection latency is
poll_interval + debounce (default ~1–1.5 s), which is fine for config/job
files that are edited by hand.

Usage:
    watcher = FileWatcher()
    watcher.watch(Path("config.yaml"), my_reload_coroutine)
    await watcher.start()
    ...
    await watcher.stop()

    # After writing a watched file yourself, call acknowledge() so the
    # watcher doesn't treat your own write as an external change:
    watcher.acknowledge(Path("jobs.yaml"))
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("pyclaw.watcher")

# (mtime_float, callback)
_WatchEntry = Tuple[float, Callable[[], Awaitable[None]]]


class FileWatcher:
    """Poll-based file watcher with debouncing.

    Args:
        poll_interval: Seconds between mtime checks (default 0.5).
        debounce: Seconds a changed mtime must be stable before the
                  callback fires (default 0.5).  This absorbs non-atomic
                  editor saves.
    """

    def __init__(self, poll_interval: float = 0.5, debounce: float = 0.5) -> None:
        self._poll_interval = poll_interval
        self._debounce = debounce
        # path → (last-known-mtime, callback)
        self._watches: Dict[Path, _WatchEntry] = {}
        # path → (candidate-mtime, time-first-detected)
        self._pending: Dict[Path, Tuple[float, float]] = {}
        self._task: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def watch(self, path: Path, callback: Callable[[], Awaitable[None]]) -> None:
        """Register *path* to be watched.  *callback* is an async callable."""
        mtime = self._safe_mtime(path)
        self._watches[path] = (mtime, callback)
        logger.debug(f"Watching: {path}")

    def unwatch(self, path: Path) -> None:
        """Stop watching *path*."""
        self._watches.pop(path, None)
        self._pending.pop(path, None)

    def acknowledge(self, path: Path) -> None:
        """Update the stored mtime for *path* to its current on-disk value.

        Call this immediately after writing a watched file yourself so the
        watcher does not treat your own write as an external change.
        """
        if path in self._watches:
            _, cb = self._watches[path]
            self._watches[path] = (self._safe_mtime(path), cb)
            self._pending.pop(path, None)

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="pyclaw-file-watcher")
        logger.info(
            f"File watcher started ({len(self._watches)} file(s), "
            f"poll={self._poll_interval}s debounce={self._debounce}s)"
        )

    async def stop(self) -> None:
        """Cancel the background polling task and wait for it to finish."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.debug("File watcher stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            return 0.0

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            now = asyncio.get_event_loop().time()

            for path, (last_mtime, callback) in list(self._watches.items()):
                current_mtime = self._safe_mtime(path)

                if current_mtime == last_mtime:
                    # File unchanged — clear any pending debounce
                    self._pending.pop(path, None)
                    continue

                # File has changed
                candidate, first_seen = self._pending.get(path, (None, None))

                if candidate != current_mtime:
                    # New or updated candidate — (re)start debounce timer
                    self._pending[path] = (current_mtime, now)
                    continue

                # Same candidate mtime; check if debounce window has elapsed
                if now - first_seen < self._debounce:
                    continue

                # Stable for debounce period — fire callback
                del self._pending[path]
                self._watches[path] = (current_mtime, callback)
                logger.info(f"Change detected: {path.name} — reloading")
                try:
                    await callback()
                except Exception as exc:
                    logger.warning(f"Reload callback error for {path.name}: {exc}")
