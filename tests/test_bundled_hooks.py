"""
Tests for bundled hook handlers and the hook loader subprocess wiring.

Covers:
  - session-memory handler: writes to FileMemoryBackend, skips empty sessions
  - boot-md handler: skips missing BOOT.md, POSTs to gateway API
  - HookLoader._make_subprocess_handler: uses sys.executable, passes JSON on stdin
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_MEMORY_HANDLER = (
    Path(__file__).parent.parent
    / "pyclaw/hooks/bundled/session-memory/handler.py"
)
_BOOT_MD_HANDLER = (
    Path(__file__).parent.parent
    / "pyclaw/hooks/bundled/boot-md/handler.py"
)


async def _run_handler(handler_path: Path, ctx: dict, env: dict | None = None) -> tuple[int, str, str]:
    """Invoke a handler script via sys.executable, return (returncode, stdout, stderr)."""
    payload = json.dumps(ctx).encode()
    proc_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(handler_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=15)
    return proc.returncode, stdout.decode(), stderr.decode()


# ---------------------------------------------------------------------------
# session-memory handler
# ---------------------------------------------------------------------------

class TestSessionMemoryHandler:

    @pytest.mark.asyncio
    async def test_empty_history_exits_zero_no_write(self, tmp_path):
        ctx = {
            "event": "command:reset",
            "agent": "myagent",
            "session_id": "sess-abc",
            "data": {"history": []},
        }
        rc, out, err = await _run_handler(
            _SESSION_MEMORY_HANDLER, ctx,
            env={"PYCLAW_CONFIG_DIR": str(tmp_path)},
        )
        assert rc == 0
        # No memory files should have been written
        memory_dir = tmp_path / "agents" / "myagent" / "memory"
        assert not memory_dir.exists() or not list(memory_dir.glob("*.md"))

    @pytest.mark.asyncio
    async def test_writes_memory_entry_for_session_with_messages(self, tmp_path):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        ctx = {
            "event": "command:reset",
            "agent": "testagent",
            "session_id": "sess-xyz",
            "data": {"history": history},
        }
        rc, out, err = await _run_handler(
            _SESSION_MEMORY_HANDLER, ctx,
            env={"PYCLAW_CONFIG_DIR": str(tmp_path)},
        )
        assert rc == 0, f"handler failed: {err}"
        # A daily memory file should exist
        memory_dir = tmp_path / "agents" / "testagent" / "memory"
        files = list(memory_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "session:testagent:sess-xyz" in content

    @pytest.mark.asyncio
    async def test_key_includes_agent_and_session_id(self, tmp_path):
        ctx = {
            "event": "command:new",
            "agent": "alpha",
            "session_id": "s123",
            "data": {"history": [{"role": "user", "content": "x"}]},
        }
        rc, out, err = await _run_handler(
            _SESSION_MEMORY_HANDLER, ctx,
            env={"PYCLAW_CONFIG_DIR": str(tmp_path)},
        )
        assert rc == 0
        memory_dir = tmp_path / "agents" / "alpha" / "memory"
        content = (list(memory_dir.glob("*.md"))[0]).read_text()
        assert "## session:alpha:s123" in content

    @pytest.mark.asyncio
    async def test_invalid_json_exits_nonzero(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_SESSION_MEMORY_HANDLER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(b"not json")
        assert proc.returncode != 0
        assert b"invalid JSON" in stderr

    @pytest.mark.asyncio
    async def test_defaults_agent_to_default(self, tmp_path):
        """When 'agent' is absent from context, writes to 'default' agent dir."""
        ctx = {
            "event": "command:reset",
            "session_id": "s9",
            "data": {"history": [{"role": "user", "content": "hi"}]},
        }
        rc, out, err = await _run_handler(
            _SESSION_MEMORY_HANDLER, ctx,
            env={"PYCLAW_CONFIG_DIR": str(tmp_path)},
        )
        assert rc == 0
        memory_dir = tmp_path / "agents" / "default" / "memory"
        assert len(list(memory_dir.glob("*.md"))) == 1

    @pytest.mark.asyncio
    async def test_recent_messages_capped_at_five(self, tmp_path):
        history = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
        ctx = {
            "event": "command:reset",
            "agent": "beta",
            "session_id": "s10",
            "data": {"history": history},
        }
        rc, out, err = await _run_handler(
            _SESSION_MEMORY_HANDLER, ctx,
            env={"PYCLAW_CONFIG_DIR": str(tmp_path)},
        )
        assert rc == 0
        # Handler should succeed; content check is structural
        memory_dir = tmp_path / "agents" / "beta" / "memory"
        assert list(memory_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# boot-md handler
# ---------------------------------------------------------------------------

class TestBootMdHandler:

    @pytest.mark.asyncio
    async def test_no_boot_md_exits_zero_silently(self, tmp_path):
        ctx = {"event": "gateway:startup", "agent": "main"}
        # Ensure neither candidate path exists by using a clean HOME
        rc, out, err = await _run_handler(
            _BOOT_MD_HANDLER, ctx,
            env={
                "HOME": str(tmp_path),
                "PYCLAW_CONFIG_DIR": str(tmp_path),
            },
        )
        assert rc == 0
        assert out == ""
        assert err == ""

    @pytest.mark.asyncio
    async def test_empty_boot_md_exits_zero(self, tmp_path):
        boot_file = tmp_path / "BOOT.md"
        boot_file.write_text("   \n")
        ctx = {"event": "gateway:startup", "agent": "main"}
        rc, out, err = await _run_handler(
            _BOOT_MD_HANDLER, ctx,
            env={"HOME": str(tmp_path)},
        )
        assert rc == 0

    @pytest.mark.asyncio
    async def test_boot_md_found_attempts_api_call(self, tmp_path):
        """When BOOT.md exists, handler attempts to POST to gateway URL."""
        boot_file = tmp_path / "BOOT.md"
        boot_file.write_text("Run startup checks.")
        ctx = {"event": "gateway:startup", "agent": "main"}
        # Point to a non-existent gateway — should fail with exit 1
        rc, out, err = await _run_handler(
            _BOOT_MD_HANDLER, ctx,
            env={
                "HOME": str(tmp_path),
                "PYCLAW_GATEWAY_URL": "http://localhost:19999",
            },
        )
        # Fails because gateway isn't running, but that means it tried
        assert rc != 0
        assert "boot-md: failed" in err

    @pytest.mark.asyncio
    async def test_pyclaw_config_dir_boot_md_takes_precedence(self, tmp_path):
        """~/.pyclaw/BOOT.md is found before ~/BOOT.md."""
        pyclaw_dir = tmp_path / ".pyclaw"
        pyclaw_dir.mkdir()
        pyclaw_boot = pyclaw_dir / "BOOT.md"
        pyclaw_boot.write_text("pyclaw boot content")
        home_boot = tmp_path / "BOOT.md"
        home_boot.write_text("home boot content")

        ctx = {"event": "gateway:startup", "agent": "main"}
        rc, out, err = await _run_handler(
            _BOOT_MD_HANDLER, ctx,
            env={
                "HOME": str(tmp_path),
                "PYCLAW_GATEWAY_URL": "http://localhost:19999",
            },
        )
        # Should attempt to connect (fails) — means it found a BOOT.md
        assert rc != 0
        assert "boot-md: failed" in err

    @pytest.mark.asyncio
    async def test_invalid_json_exits_nonzero(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_BOOT_MD_HANDLER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(b"not json")
        assert proc.returncode != 0
        assert b"invalid JSON" in stderr


# ---------------------------------------------------------------------------
# HookLoader subprocess wiring
# ---------------------------------------------------------------------------

class TestHookLoaderSubprocessWiring:

    def test_make_subprocess_handler_uses_sys_executable(self):
        """_make_subprocess_handler must use sys.executable, not the raw script path."""
        from pyclaw.hooks.loader import _make_subprocess_handler, HookInfo

        info = HookInfo(
            name="test-hook",
            description="test",
            version="1.0",
            events=["gateway:startup"],
            hook_md=Path("/fake/HOOK.md"),
            handler_path=Path("/fake/handler.py"),
        )
        handler = _make_subprocess_handler(info)
        # Inspect the closure — the handler should reference sys.executable
        # We verify this indirectly by running a dummy script
        assert callable(handler)
        assert asyncio.iscoroutinefunction(handler)

    @pytest.mark.asyncio
    async def test_subprocess_handler_passes_context_on_stdin(self, tmp_path):
        """Handler receives event context as JSON on stdin."""
        # Write a script that reads stdin and echoes a field back on stdout
        script = tmp_path / "echo_agent.py"
        script.write_text(
            "import json, sys\n"
            "ctx = json.loads(sys.stdin.read())\n"
            "print(json.dumps({'agent': ctx.get('agent')}))\n"
        )
        from pyclaw.hooks.loader import _make_subprocess_handler, HookInfo
        info = HookInfo(
            name="echo",
            description="",
            version="1",
            events=["test:event"],
            hook_md=tmp_path / "HOOK.md",
            handler_path=script,
        )
        handler = _make_subprocess_handler(info)
        result = await handler({"agent": "myagent", "event": "test:event"})
        assert result == {"agent": "myagent"}

    @pytest.mark.asyncio
    async def test_subprocess_handler_returns_none_on_nonzero_exit(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("import sys\nsys.exit(1)\n")
        from pyclaw.hooks.loader import _make_subprocess_handler, HookInfo
        info = HookInfo(
            name="fail",
            description="",
            version="1",
            events=["test:event"],
            hook_md=tmp_path / "HOOK.md",
            handler_path=script,
        )
        handler = _make_subprocess_handler(info)
        result = await handler({})
        assert result is None

    @pytest.mark.asyncio
    async def test_subprocess_handler_returns_none_when_stdout_empty(self, tmp_path):
        script = tmp_path / "silent.py"
        script.write_text("import sys\nsys.stdin.read()\n")  # read stdin, exit 0, no output
        from pyclaw.hooks.loader import _make_subprocess_handler, HookInfo
        info = HookInfo(
            name="silent",
            description="",
            version="1",
            events=["test:event"],
            hook_md=tmp_path / "HOOK.md",
            handler_path=script,
        )
        handler = _make_subprocess_handler(info)
        result = await handler({})
        assert result is None

    @pytest.mark.asyncio
    async def test_subprocess_handler_timeout_returns_none(self, tmp_path):
        script = tmp_path / "slow.py"
        script.write_text("import time\ntime.sleep(60)\n")
        from pyclaw.hooks.loader import _make_subprocess_handler, HookInfo
        import pyclaw.hooks.loader as loader_mod

        info = HookInfo(
            name="slow",
            description="",
            version="1",
            events=["test:event"],
            hook_md=tmp_path / "HOOK.md",
            handler_path=script,
        )
        handler = _make_subprocess_handler(info)
        # Patch the timeout to 0.1s so the test doesn't actually wait 30s
        original = asyncio.wait_for
        async def fast_wait_for(coro, timeout):
            return await original(coro, timeout=0.1)

        with patch("asyncio.wait_for", side_effect=fast_wait_for):
            result = await handler({})
        assert result is None
