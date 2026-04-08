"""Singleton gateway lock — prevents multiple pyclopse instances from running.

Uses ``fcntl.flock()`` on ``~/.pyclopse/gateway.lock``.  The OS releases the
lock automatically when the process exits (even on SIGKILL), so there are no
stale-lock problems.

Usage::

    from pyclopse.core.singleton import acquire_gateway_lock

    lock = acquire_gateway_lock()   # raises GatewayAlreadyRunning on conflict
    # … run gateway …
    # lock is released automatically on process exit
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger("pyclopse.singleton")

LOCK_PATH = Path("~/.pyclopse/gateway.lock").expanduser()

# Module-level fd so the lock is held for the lifetime of the process
# and a second call within the same process is a harmless no-op.
_lock_fd: int | None = None


class GatewayAlreadyRunning(RuntimeError):
    """Raised when another pyclopse gateway process holds the lock."""


def acquire_gateway_lock() -> int:
    """Acquire an exclusive lock on the gateway lock file.

    Returns the file descriptor (kept open for the lifetime of the process).
    The lock is released automatically when the process exits.

    Raises:
        GatewayAlreadyRunning: If another process already holds the lock.
    """
    global _lock_fd
    if _lock_fd is not None:
        return _lock_fd  # already locked by this process

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Open (or create) the lock file.  O_RDWR so flock works on all platforms.
    fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise GatewayAlreadyRunning(
            "Another pyclopse gateway is already running. "
            "Stop it first with: pyclopse service stop"
        )

    # Write our PID for informational purposes (not used for locking)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())

    _lock_fd = fd
    logger.info("Gateway lock acquired (pid=%d)", os.getpid())
    return fd
