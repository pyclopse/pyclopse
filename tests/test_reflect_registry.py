"""Tests for the pyclaw reflection registry and decorators."""

import pytest
from pyclaw.reflect.registry import (
    CATEGORY_COMMAND,
    CATEGORY_EVENT,
    CATEGORY_SYSTEM,
    Entry,
    get_registry,
    query,
    register,
    _registry,
)
from pyclaw.reflect.decorators import (
    reflect_command,
    reflect_event,
    reflect_system,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — isolated classes so tests don't pollute the global registry
# ─────────────────────────────────────────────────────────────────────────────

def _unique(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# register() basics
# ─────────────────────────────────────────────────────────────────────────────

def test_register_creates_entry():
    name = _unique("test-sys")

    class _Sys:
        """A test system."""

    register(CATEGORY_SYSTEM, name, _Sys)
    reg = get_registry()
    assert name in reg[CATEGORY_SYSTEM]
    entries = reg[CATEGORY_SYSTEM][name]
    assert len(entries) == 1
    assert isinstance(entries[0], Entry)
    assert entries[0].docstring == "A test system."
    assert "_Sys" in entries[0].signature
    assert "class" in entries[0].signature


def test_register_multiple_same_name_merges():
    name = _unique("merged")

    class _Part1:
        """Part one doc."""

    class _Part2:
        """Part two doc."""

    register(CATEGORY_SYSTEM, name, _Part1)
    register(CATEGORY_SYSTEM, name, _Part2)

    entries = get_registry()[CATEGORY_SYSTEM][name]
    assert len(entries) == 2


def test_register_invalid_category_raises():
    with pytest.raises(ValueError, match="Unknown reflection category"):
        register("bogus", "x", object)


def test_register_function():
    name = _unique("cmd")

    async def _handler(args, ctx):
        """Handle stuff."""

    register(CATEGORY_COMMAND, name, _handler)
    entries = get_registry()[CATEGORY_COMMAND][name]
    assert len(entries) >= 1
    assert "async def" in entries[-1].signature


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def test_reflect_system_decorator_transparent():
    name = _unique("sys-dec")

    @reflect_system(name)
    class _MySystem:
        """My system docstring."""

        def method(self):
            pass

    # Decorator must return the class unchanged
    assert _MySystem.__name__ == "_MySystem"
    assert hasattr(_MySystem, "method")
    assert _MySystem.__doc__ == "My system docstring."


def test_reflect_system_registers():
    name = _unique("sys-reg")

    @reflect_system(name)
    class _Reg:
        """Reg doc."""

    assert name in get_registry().get(CATEGORY_SYSTEM, {})


def test_reflect_event_decorator():
    name = _unique("evt")

    @reflect_event(name, payload={"id": "str", "result": "str"})
    class _Evt:
        """Event docstring."""

    entries = get_registry()[CATEGORY_EVENT][name]
    assert len(entries) >= 1
    assert entries[-1].payload == {"id": "str", "result": "str"}


def test_reflect_command_decorator():
    cmd_name = _unique("/cmd")

    @reflect_command(cmd_name)
    async def _cmd(args, ctx):
        """Command doc."""

    # Must return original function
    assert callable(_cmd)
    assert _cmd.__name__ == "_cmd"
    entries = get_registry()[CATEGORY_COMMAND][cmd_name]
    assert len(entries) >= 1


def test_reflect_command_function_call_form():
    """reflect_command("name")(fn) form for inner functions."""
    cmd_name = _unique("/inner")

    async def _inner(args, ctx):
        """Inner command."""

    reflect_command(cmd_name)(_inner)
    assert cmd_name in get_registry().get(CATEGORY_COMMAND, {})


# ─────────────────────────────────────────────────────────────────────────────
# query()
# ─────────────────────────────────────────────────────────────────────────────

def test_query_no_args_returns_overview():
    result = query()
    assert "pyclaw" in result.lower()
    # Should mention the architecture
    assert "Gateway" in result or "gateway" in result


def test_query_category_only_lists_entries():
    name = _unique("listsys")

    @reflect_system(name)
    class _ListSys:
        """Listed system."""

    result = query(category="system")
    assert name in result
    assert "Listed system." in result


def test_query_category_and_name_returns_detail():
    name = _unique("detail")

    @reflect_system(name)
    class _DetailSys:
        """Detailed docstring for this system."""

        def important_method(self):
            pass

    result = query(category="system", name=name)
    assert name in result
    assert "Detailed docstring" in result
    assert "_DetailSys" in result


def test_query_name_only_cross_category_search():
    name = _unique("cross")

    @reflect_system(name)
    class _CrossSys:
        """Cross system."""

    result = query(name=name)
    assert name in result
    assert "system" in result


def test_query_not_found_returns_not_found():
    result = query(category="system", name="definitely-does-not-exist-xyz")
    assert "[NOT FOUND]" in result


def test_query_name_only_not_found():
    result = query(name="definitely-does-not-exist-xyz-abc")
    assert "[NOT FOUND]" in result


def test_query_config_list():
    result = query(category="config")
    assert "gateway" in result
    assert "agents" in result


def test_query_config_detail():
    result = query(category="config", name="agents")
    assert "AgentConfig" in result
    assert "model" in result


def test_query_config_unknown_section():
    result = query(category="config", name="nonexistent-section")
    assert "[NOT FOUND]" in result


def test_query_merged_docstring():
    name = _unique("merge-test")

    @reflect_system(name)
    class _MergeA:
        """Section A of documentation."""

    @reflect_system(name)
    class _MergeB:
        """Section B of documentation."""

    result = query(category="system", name=name)
    assert "Section A" in result
    assert "Section B" in result
    # Should include separator when merging
    assert "---" in result
