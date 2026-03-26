"""Source file reader for the pyclopse reflection system.

Reads pyclopse source files with line numbers, for use by the
``reflect_source()`` MCP tool and the ``/api/v1/reflect/source/`` REST route.
"""

from __future__ import annotations

from pathlib import Path

# pyclopse package root — resolved relative to this file so it works in both
# dev checkouts and uv-tool-installed environments.
_PACKAGE_DIR = Path(__file__).parent.parent


def source_file(module: str) -> str:
    """Read a pyclopse source file with line numbers.

    *module* is a path relative to the pyclopse package root, e.g.
    ``'core/gateway.py'`` or ``'agents/runner.py'``. Only paths within the
    pyclopse package are accessible; directory traversal is rejected.

    Returns the file contents with ``lineno\\t`` prefixes, or a descriptive
    error/not-found string.

    Args:
        module: Forward-slash path relative to the pyclopse package root.
                Example: ``'core/gateway.py'``, ``'reflect/registry.py'``

    Returns:
        str: Numbered source text, a directory listing, or an error message.
    """
    module = module.strip().lstrip("/")

    resolved = (_PACKAGE_DIR / module).resolve()
    package_root = _PACKAGE_DIR.resolve()

    if not str(resolved).startswith(str(package_root)):
        return "[ERROR] Path escapes the pyclopse package directory."

    if not resolved.exists():
        return (
            f"[NOT FOUND] '{module}' not found in pyclopse package.\n"
            f"Package root: {package_root}\n"
            "Check the path — use forward slashes, no leading slash.\n"
            "Example: reflect_source('core/gateway.py')"
        )

    if not resolved.is_file():
        entries = sorted(resolved.iterdir())
        lines = [f"'{module}' is a directory. Contents:"]
        for e in entries:
            suffix = "/" if e.is_dir() else ""
            lines.append(f"  {e.name}{suffix}")
        return "\n".join(lines)

    raw = resolved.read_text(encoding="utf-8")
    text_lines = raw.splitlines()
    width = len(str(len(text_lines)))
    numbered = "\n".join(
        f"{str(i + 1).rjust(width)}\t{line}" for i, line in enumerate(text_lines)
    )
    return f"# {module}\n\n{numbered}"
