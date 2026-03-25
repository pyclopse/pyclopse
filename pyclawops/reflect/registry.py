"""Global reflection registry for pyclawops.

Populated at import time via @reflect_system / @reflect_event / @reflect_command
decorators.  Queried by the ``reflect`` MCP tool.
"""

from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_SYSTEM = "system"
CATEGORY_EVENT = "event"
CATEGORY_COMMAND = "command"

VALID_CATEGORIES = {CATEGORY_SYSTEM, CATEGORY_EVENT, CATEGORY_COMMAND}


@dataclass
class Entry:
    obj: Any
    name: str                    # e.g. "gateway", "job:complete", "/reset"
    category: str                # "system" | "event" | "command"
    docstring: str               # cleaned docstring of obj
    signature: str               # e.g. "class Gateway" or "async def cmd_reset(args, ctx)"
    source_file: str
    source_line: int
    payload: dict[str, Any] = field(default_factory=dict)


# _registry[category][name] = list[Entry]  (multiple per name → merged docstring)
_registry: dict[str, dict[str, list[Entry]]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def register(category: str, name: str, obj: Any, payload: dict[str, Any] | None = None) -> None:
    """Register *obj* under (*category*, *name*).  Called by decorators."""
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Unknown reflection category {category!r}")

    try:
        src_file = inspect.getfile(obj)
    except (TypeError, OSError):
        src_file = "<unknown>"
    try:
        src_line = inspect.getsourcelines(obj)[1]
    except (TypeError, OSError):
        src_line = 0

    raw_doc = inspect.getdoc(obj) or ""
    sig = _build_signature(obj)

    entry = Entry(
        obj=obj,
        name=name,
        category=category,
        docstring=raw_doc,
        signature=sig,
        source_file=src_file,
        source_line=src_line,
        payload=payload or {},
    )

    _registry.setdefault(category, {}).setdefault(name, []).append(entry)


def _build_signature(obj: Any) -> str:
    if inspect.isclass(obj):
        return f"class {obj.__qualname__}"
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        try:
            sig = inspect.signature(obj)
            prefix = "async def" if inspect.iscoroutinefunction(obj) else "def"
            return f"{prefix} {obj.__qualname__}{sig}"
        except (ValueError, TypeError):
            return f"def {obj.__qualname__}(...)"
    return repr(obj)


# ─────────────────────────────────────────────────────────────────────────────
# Accessors
# ─────────────────────────────────────────────────────────────────────────────

def get_registry() -> dict[str, dict[str, list[Entry]]]:
    return _registry


def _merge_entries(entries: list[Entry]) -> str:
    """Merge docstrings from multiple entries registered under the same name."""
    if len(entries) == 1:
        return entries[0].docstring or "(no docstring)"

    # Sort deterministically so merged output is stable
    sorted_entries = sorted(entries, key=lambda e: (e.source_file, e.source_line))
    parts: list[str] = []
    for e in sorted_entries:
        header = f"**{e.signature}**"
        body = e.docstring or "(no docstring)"
        parts.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(parts)


def query(category: str | None = None, name: str | None = None) -> str:
    """Query the reflection registry.

    - no args          → module-level architecture overview (pyclawops.__doc__)
    - category only    → list all names in that category
    - name only        → cross-category search for matching name
    - category + name  → full detail for that entry
    """
    # ── overview ────────────────────────────────────────────────────────────
    if category is None and name is None:
        import pyclawops  # lazy — avoids circular import
        overview = inspect.getdoc(pyclawops) or "No overview available."
        return overview

    # ── full detail ─────────────────────────────────────────────────────────
    if category is not None and name is not None:
        cat_norm = category.lower()
        name_norm = name.lower()

        # Special-case: config category delegates to Pydantic schema
        if cat_norm == "config":
            return _query_config(name_norm)

        entries = _registry.get(cat_norm, {}).get(name_norm, [])
        if not entries:
            return f"[NOT FOUND] No {cat_norm!r} entry named {name_norm!r}."

        e0 = entries[0]
        merged_doc = _merge_entries(entries)
        lines = [
            f"# {cat_norm}: {name_norm}",
            "",
            f"**{e0.signature}**",
            f"*{e0.source_file}:{e0.source_line}*",
            "",
            merged_doc,
        ]
        if e0.payload:
            lines += ["", "**Metadata:**", _fmt_dict(e0.payload)]
        return "\n".join(lines)

    # ── list category ───────────────────────────────────────────────────────
    if category is not None and name is None:
        cat_norm = category.lower()
        if cat_norm == "config":
            return _list_config_sections()
        names = sorted(_registry.get(cat_norm, {}).keys())
        if not names:
            return f"No entries registered under category {cat_norm!r}."
        lines = [f"# {cat_norm}s ({len(names)} registered)"]
        for n in names:
            entries = _registry[cat_norm][n]
            first_line = (entries[0].docstring or "").split("\n")[0]
            lines.append(f"- **{n}** — {first_line}")
        return "\n".join(lines)

    # ── cross-category name search ───────────────────────────────────────────
    # name only, category is None
    name_norm = (name or "").lower()
    results: list[str] = []
    for cat, names_dict in sorted(_registry.items()):
        if name_norm in names_dict:
            entries = names_dict[name_norm]
            merged_doc = _merge_entries(entries)
            first_line = merged_doc.split("\n")[0]
            results.append(f"- **{cat}/{name_norm}**: {first_line}")
    if not results:
        return f"[NOT FOUND] No entries matching name {name_norm!r} in any category."
    lines = [f"# Search results for {name_norm!r}", ""] + results
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Config reflection helpers
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_SECTION_MAP: dict[str, str] = {
    "gateway": "GatewayConfig",
    "agents": "AgentConfig",
    "providers": "ProviderConfig",
    "channels": "ChannelConfig",
    "memory": "MemoryConfig",
    "security": "SecurityConfig",
    "sessions": "SessionsConfig",
    "jobs": "JobConfig",
}


def _list_config_sections() -> str:
    sections = sorted(_CONFIG_SECTION_MAP.keys())
    lines = ["# config sections"] + [f"- **{s}**" for s in sections]
    return "\n".join(lines)


def _query_config(section: str) -> str:
    from pyclawops.config import schema as cfg_schema  # lazy import

    class_name = _CONFIG_SECTION_MAP.get(section)
    if not class_name:
        avail = ", ".join(sorted(_CONFIG_SECTION_MAP))
        return f"[NOT FOUND] Unknown config section {section!r}. Available: {avail}"

    cls = getattr(cfg_schema, class_name, None)
    if cls is None:
        return f"[NOT FOUND] Config class {class_name!r} not found in schema."

    try:
        json_schema = cls.model_json_schema()
    except Exception as exc:
        return f"[ERROR] Could not extract schema for {class_name}: {exc}"

    lines = [f"# config: {section}  ({class_name})"]
    props = json_schema.get("properties", {})
    for field_name, field_info in sorted(props.items()):
        ftype = field_info.get("type") or field_info.get("anyOf", [{}])[0].get("type", "?")
        fdesc = field_info.get("description", "")
        fdefault = field_info.get("default", "")
        line = f"- **{field_name}** ({ftype})"
        if fdesc:
            line += f": {fdesc}"
        if fdefault not in ("", None):
            line += f"  *(default: {fdefault!r})*"
        lines.append(line)

    if not props:
        doc = inspect.getdoc(cls) or ""
        lines.append(doc or "(no fields documented)")

    return "\n".join(lines)


def _fmt_dict(d: dict) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in d.items())
