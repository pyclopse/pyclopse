"""File-based hook discovery and loading (HOOK.md + handler script)."""

import asyncio
from pyclawops.reflect import reflect_system
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .registry import HookRegistry

logger = logging.getLogger("pyclawops.hooks")

# Bundled hooks shipped with pyclawops
_BUNDLED_DIR = Path(__file__).parent / "bundled"


@dataclass
class HookInfo:
    """Metadata parsed from a hook's HOOK.md frontmatter file.

    Attributes:
        name (str): Unique hook name derived from the YAML ``name`` field or
            the parent directory name.
        description (str): Human-readable description of what the hook does.
        version (str): Version string from the HOOK.md frontmatter.
        events (List[str]): List of event names this hook subscribes to.
        hook_md (Path): Absolute path to the HOOK.md file.
        handler_path (Optional[Path]): Absolute path to the handler script.
            None if the file is missing or not declared.
        requirements (Dict[str, Any]): Optional requirements dict from frontmatter
            (e.g., config keys the hook needs). Defaults to empty dict.
        source (str): Origin label — "bundled", "managed", or "workspace".
            Defaults to "managed".
        enabled (bool): Whether the hook should be registered. Defaults to True.
    """

    name: str
    description: str
    version: str
    events: List[str]
    hook_md: Path                           # absolute path to HOOK.md
    handler_path: Optional[Path] = None     # absolute path to handler script
    requirements: Dict[str, Any] = field(default_factory=dict)
    source: str = "managed"                 # "bundled" | "managed" | "workspace"
    enabled: bool = True


@reflect_system("hooks")
class HookLoader:
    """
    Discovers HOOK.md files and registers subprocess-backed handlers.

    Search order (later sources override earlier on name collision):
      1. Bundled  — pyclawops/hooks/bundled/
      2. Managed  — ~/.pyclawops/hooks/
      3. Extra    — gateway.hooks_dirs entries in config

    Each hook directory must contain a HOOK.md file with YAML frontmatter:

        ---
        name: session-memory
        description: Save session to memory on /new or /reset
        version: 1.0.0
        events:
          - command:new
          - command:reset
        handler: handler.py       # relative to the hook directory
        requirements:
          config:
            - sessions.persist_dir
        ---

    The handler is invoked as a subprocess.  Event context is passed as
    JSON on stdin.  For interceptable events (memory:*) the handler should
    write a JSON result to stdout; for notification events stdout is
    ignored.
    """

    def __init__(
        self,
        config_dir: str = "~/.pyclawops",
        extra_dirs: Optional[List[str]] = None,
    ) -> None:
        """Initialise the HookLoader with search directories.

        Args:
            config_dir (str): Path to the pyclawops config directory. The managed
                hooks sub-directory (``{config_dir}/hooks``) is automatically
                included in the search path. Defaults to "~/.pyclawops".
            extra_dirs (Optional[List[str]]): Additional hook search paths
                (workspace hooks). These override bundled and managed hooks on
                name collision. Defaults to None.
        """
        self._config_dir = Path(config_dir).expanduser()
        self._extra_dirs = [Path(d).expanduser() for d in (extra_dirs or [])]

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def discover(self) -> List[HookInfo]:
        """
        Scan all hook directories and return a de-duplicated list of HookInfo.

        Later sources win on name collision (extra_dirs > managed > bundled).
        """
        found: Dict[str, HookInfo] = {}

        for root, source_label in self._search_dirs():
            if not root.exists():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                hook_md = entry / "HOOK.md"
                if not hook_md.exists():
                    continue
                info = self._parse_hook_md(hook_md, source_label)
                if info is not None:
                    found[info.name] = info

        return list(found.values())

    def _search_dirs(self):
        """Yield (path, label) tuples in ascending override priority.

        Yields bundled hooks first, then managed hooks, then any extra
        (workspace) dirs. Later entries win on name collision in ``discover()``.

        Yields:
            Tuple[Path, str]: (directory path, source label string) pairs.
        """
        yield _BUNDLED_DIR, "bundled"
        yield self._config_dir / "hooks", "managed"
        for d in self._extra_dirs:
            yield d, "workspace"

    def _parse_hook_md(self, hook_md: Path, source: str) -> Optional[HookInfo]:
        """Parse a HOOK.md file and return a HookInfo instance, or None on error.

        Reads and splits the YAML frontmatter from the Markdown body, validates
        required fields, and resolves the handler script path relative to the
        hook directory.

        Args:
            hook_md (Path): Absolute path to the HOOK.md file to parse.
            source (str): Source label string ("bundled", "managed", "workspace").

        Returns:
            Optional[HookInfo]: Populated HookInfo on success, or None if the
                file cannot be read, has no frontmatter, or has a YAML parse error.
        """
        try:
            raw = hook_md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Cannot read {hook_md}: {exc}")
            return None

        frontmatter, _ = _split_frontmatter(raw)
        if frontmatter is None:
            logger.warning(f"No YAML frontmatter in {hook_md}")
            return None

        try:
            meta = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            logger.warning(f"YAML parse error in {hook_md}: {exc}")
            return None

        name = meta.get("name", hook_md.parent.name)
        events = meta.get("events", [])
        if isinstance(events, str):
            events = [events]

        handler_rel = meta.get("handler")
        handler_path: Optional[Path] = None
        if handler_rel:
            candidate = hook_md.parent / handler_rel
            if candidate.exists():
                handler_path = candidate
            else:
                logger.warning(
                    f"Hook '{name}' declares handler '{handler_rel}' "
                    f"but file not found at {candidate}"
                )

        return HookInfo(
            name=name,
            description=meta.get("description", ""),
            version=str(meta.get("version", "")),
            events=events,
            hook_md=hook_md,
            handler_path=handler_path,
            requirements=meta.get("requirements", {}),
            source=source,
            enabled=meta.get("enabled", True),
        )

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_all(
        self,
        registry: HookRegistry,
        enabled_names: Optional[List[str]] = None,
    ) -> int:
        """
        Discover all hooks and register enabled ones into *registry*.

        Args:
            registry:      Target HookRegistry.
            enabled_names: If provided, only hooks whose name is in this list
                           are registered.  Bundled hooks with no entry in the
                           list are skipped.  Pass None to register all.

        Returns:
            Number of hooks registered.
        """
        count = 0
        for info in self.discover():
            if not info.enabled:
                continue
            if enabled_names is not None and info.name not in enabled_names:
                logger.debug(f"Skipping hook '{info.name}' (not in enabled list)")
                continue
            if info.handler_path is None:
                logger.warning(
                    f"Hook '{info.name}' has no valid handler — skipping"
                )
                continue

            handler = _make_subprocess_handler(info)
            for event in info.events:
                registry.register(
                    event=event,
                    handler=handler,
                    description=info.description,
                    source=f"file:{info.hook_md}",
                )
            logger.info(
                f"Registered file hook '{info.name}' "
                f"({source_label(info)}) for events: {info.events}"
            )
            count += 1
        return count


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def source_label(info: HookInfo) -> str:
    """Return the source label string for a HookInfo instance.

    Args:
        info (HookInfo): The hook info whose source label is requested.

    Returns:
        str: The ``source`` attribute of the HookInfo (e.g., "bundled",
            "managed", or "workspace").
    """
    return info.source


def _split_frontmatter(text: str):
    """
    Split a Markdown document into (frontmatter_yaml, body).

    Returns (None, text) if no frontmatter block is found.
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    frontmatter = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    return frontmatter, body


def _make_subprocess_handler(info: HookInfo) -> Callable:
    """
    Return an async callable that invokes the hook's handler script.

    Context is written to the process stdin as JSON.  For interceptable
    events the handler should write a JSON result to stdout; for
    notification events stdout is ignored.
    """
    import sys as _sys
    handler_path = info.handler_path
    name = info.name

    async def _handler(context: Dict[str, Any]) -> Any:
        """Invoke the hook script as a subprocess with JSON context on stdin.

        Serialises ``context`` to JSON, writes it to the process stdin, and
        waits up to 30 seconds for the process to complete. Non-zero exit codes
        and timeouts are logged as errors. For interceptable events the handler
        parses stdout as JSON and returns the result; for notification events
        stdout is ignored and None is returned.

        Args:
            context (Dict[str, Any]): Event context dictionary passed to the
                handler script via stdin.

        Returns:
            Any: Parsed JSON from stdout if the script produces valid JSON output
                and exits with code 0; None otherwise.
        """
        payload = json.dumps(context).encode()
        try:
            proc = await asyncio.create_subprocess_exec(
                _sys.executable, str(handler_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload),
                timeout=30,
            )
            if proc.returncode != 0:
                logger.error(
                    f"Hook '{name}' exited {proc.returncode}: "
                    f"{stderr.decode().strip()}"
                )
                return None
            if stdout.strip():
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    pass
        except asyncio.TimeoutError:
            logger.error(f"Hook '{name}' timed out")
        except Exception as exc:
            logger.error(f"Hook '{name}' failed: {exc}", exc_info=True)
        return None

    _handler.__name__ = f"hook:{name}"
    return _handler
