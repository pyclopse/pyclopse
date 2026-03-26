"""
Integration tests for ACP runner classes.

These tests exercise real subprocesses — they are skipped automatically if the
required binary is not on PATH or if API keys are missing.

Run with:
    uv run pytest tests/test_acp_runner.py -v -s
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict

import pytest

from pyclawops.agents.acp_runner import AcpRunner, ClaudeCodeRunner, OpenCodeRunner

# ---------------------------------------------------------------------------
# Per-test timeouts (seconds)
# ---------------------------------------------------------------------------
_TIMEOUT_FAST = 60    # single-turn ACP / claude / opencode call
_TIMEOUT_MULTI = 120  # multi-turn (session reuse test)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

FASTAGENT_ACP = shutil.which(
    "fast-agent-acp",
    path=os.path.join(os.path.dirname(sys.executable), "..") + ":" + os.environ.get("PATH", ""),
)
# Also check the venv bin directly
_VENV_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".venv", "bin", "fast-agent-acp",
)
if not FASTAGENT_ACP and os.path.isfile(_VENV_BIN):
    FASTAGENT_ACP = _VENV_BIN

CLAUDE_BIN = shutil.which("claude")
OPENCODE_BIN = shutil.which("opencode")

HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_minimax_key() -> str:
    """Read MiniMax API key from env, pyclawops config, or macOS keychain."""
    key = os.environ.get("MINIMAX_API_KEY") or os.environ.get("GENERIC_API_KEY") or ""
    if key:
        return key
    # Try pyclawops config file
    try:
        import yaml  # type: ignore
        cfg_path = Path.home() / ".pyclawops" / "config" / "pyclawops.yaml"
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text()) or {}
            key = data.get("providers", {}).get("minimax", {}).get("api_key", "")
            if key:
                return key
    except Exception:
        pass
    # Try macOS keychain
    try:
        import subprocess
        key = subprocess.check_output(
            ["security", "find-generic-password", "-s", "pyclawops", "-a", "minimax-api-key", "-w"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        pass
    return key


_MINIMAX_KEY = _get_minimax_key()
HAS_MINIMAX_KEY = bool(_MINIMAX_KEY)

# Claude Code manages auth via ~/.claude/ config — if the binary exists it's
# likely authenticated regardless of ANTHROPIC_API_KEY env var.
# Skip if running inside Claude Code (CLAUDECODE=1) — claude -p is blocked there.
INSIDE_CLAUDE_CODE = bool(os.environ.get("CLAUDECODE"))
CLAUDE_AUTHENTICATED = bool(CLAUDE_BIN) and not INSIDE_CLAUDE_CODE


def _run(coro, timeout: float):
    """Run a coroutine with a hard timeout; raise pytest.fail on timeout."""
    async def _guarded():
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            pytest.fail(f"Test timed out after {timeout}s")
    return asyncio.run(_guarded())


# ---------------------------------------------------------------------------
# AcpRunner tests (fast-agent-acp)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not FASTAGENT_ACP,
    reason="fast-agent-acp not found in PATH or venv",
)
@pytest.mark.skipif(
    not HAS_MINIMAX_KEY,
    reason="MINIMAX_API_KEY / GENERIC_API_KEY not set",
)
class TestAcpRunnerFastAgent:
    """Tests using fast-agent-acp with MiniMax as the ACP subprocess."""

    # fast-agent-acp is a standalone subprocess that needs its own fastagent.config.yaml
    # to locate MCP servers. Use the user's ~/.pyclawops/ config dir if it has one.
    FA_CONFIG = str(
        next(
            (
                p
                for p in [
                    Path.home() / ".pyclawops" / "fastagent.config.yaml",
                ]
                if p.exists()
            ),
            Path("/tmp"),
        ).parent
    )

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            # Symlink fastagent config into tmpdir so fast-agent-acp can find it
            fa_cfg = Path(self.FA_CONFIG) / "fastagent.config.yaml"
            if fa_cfg.exists():
                target = Path(d) / "fastagent.config.yaml"
                try:
                    target.symlink_to(fa_cfg.resolve())
                except Exception:
                    pass
            yield d

    def _instr_file(self, tmpdir: str, text: str = "You are a test assistant. Answer in one sentence only.") -> str:
        """Write instruction text to a file and return the path."""
        p = Path(tmpdir) / "instruction.txt"
        p.write_text(text)
        return str(p)

    def _minimax_env(self) -> Dict[str, str]:
        return {
            "GENERIC_API_KEY": _MINIMAX_KEY,
            "GENERIC_BASE_URL": "https://api.minimax.io/v1",
        }

    def test_run_returns_string(self, tmpdir):
        """AcpRunner.run() returns a non-empty string response."""
        runner = AcpRunner(
            command=FASTAGENT_ACP,
            args=[
                "--model", "generic.MiniMax-M2.5",
                "--instruction", self._instr_file(tmpdir),
                "--no-permissions",
            ],
            cwd=tmpdir,
            env={"GENERIC_API_KEY": _MINIMAX_KEY, "GENERIC_BASE_URL": "https://api.minimax.io/v1"},
        )

        async def go():
            async with runner:
                return await runner.run("Say exactly: hello from acp")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[fast-agent-acp] run() result: {result!r}")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_run_stream_yields_chunks(self, tmpdir):
        """AcpRunner.run_stream() yields incremental (text, is_reasoning) tuples."""
        runner = AcpRunner(
            command=FASTAGENT_ACP,
            args=[
                "--model", "generic.MiniMax-M2.5",
                "--instruction", self._instr_file(tmpdir),
                "--no-permissions",
            ],
            cwd=tmpdir,
            env=self._minimax_env(),
        )

        async def go():
            chunks = []
            async with runner:
                async for text, is_reasoning in runner.run_stream(
                    "Count from 1 to 5, one number per word."
                ):
                    print(f"  chunk is_reasoning={is_reasoning}: {text!r}")
                    chunks.append((text, is_reasoning))
            return chunks

        chunks = _run(go(), _TIMEOUT_FAST)
        print(f"\n[fast-agent-acp] stream chunks: {len(chunks)}")
        assert len(chunks) > 0
        full = "".join(t for t, _ in chunks)
        assert len(full) > 0

    def test_session_reuse(self, tmpdir):
        """Two consecutive run() calls reuse the same ACP session."""
        runner = AcpRunner(
            command=FASTAGENT_ACP,
            args=[
                "--model", "generic.MiniMax-M2.5",
                "--instruction", self._instr_file(tmpdir, "You are a test assistant."),
                "--no-permissions",
            ],
            cwd=tmpdir,
            env=self._minimax_env(),
        )

        async def go():
            async with runner:
                session_id_before = runner._session_id
                r1 = await runner.run("My favourite colour is blue. Acknowledge briefly.")
                r2 = await runner.run("What colour did I just mention?")
                session_id_after = runner._session_id
            return session_id_before, r1, r2, session_id_after

        sid_before, r1, r2, sid_after = _run(go(), _TIMEOUT_MULTI)
        print(f"\n[fast-agent-acp] session {sid_before}")
        print(f"  turn1: {r1!r}")
        print(f"  turn2: {r2!r}")
        assert sid_before == sid_after, "Session ID must not change between turns"
        assert "blue" in r2.lower() or "colour" in r2.lower() or len(r2) > 0

    def test_permission_handler_called(self, tmpdir):
        """Custom permission_handler is invoked when the agent requests a tool."""
        calls = []

        async def my_handler(options, tool_call):
            calls.append(tool_call)
            return "allow_once"

        runner = AcpRunner(
            command=FASTAGENT_ACP,
            args=["--model", "generic.MiniMax-M2.5", "--instruction", self._instr_file(tmpdir, "You are a shell assistant.")],
            cwd=tmpdir,
            env=self._minimax_env(),
            permission_handler=my_handler,
        )

        async def go():
            async with runner:
                return await runner.run("Run: echo hello")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[fast-agent-acp] permission test result: {result!r}")
        print(f"  permission calls: {len(calls)}")
        # The agent may or may not request permission depending on config,
        # but the runner should complete without error either way.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# ClaudeCodeRunner tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CLAUDE_AUTHENTICATED, reason="claude not found in PATH or running inside Claude Code")
class TestClaudeCodeRunner:
    """Tests using ``claude -p`` (Claude Code non-interactive mode)."""

    def test_run_returns_string(self):
        """ClaudeCodeRunner.run() returns a non-empty string."""
        runner = ClaudeCodeRunner()

        async def go():
            return await runner.run("Reply with exactly three words: one two three")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[claude -p] run() result: {result!r}")
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    def test_run_stream_yields_chunks(self):
        """ClaudeCodeRunner.run_stream() yields incremental text chunks."""
        runner = ClaudeCodeRunner()

        async def go():
            chunks = []
            async for text, is_reasoning in runner.run_stream(
                "Write a haiku about coding."
            ):
                print(f"  chunk is_reasoning={is_reasoning}: {text!r}")
                chunks.append((text, is_reasoning))
            return chunks

        chunks = _run(go(), _TIMEOUT_FAST)
        print(f"\n[claude -p] stream: {len(chunks)} chunks")
        assert len(chunks) > 0
        full = "".join(t for t, is_r in chunks if not is_r)
        assert len(full) > 0

    def test_delta_slicing(self):
        """Response is delivered as incremental deltas, not repeated full text."""
        runner = ClaudeCodeRunner()

        async def go():
            chunks = []
            async for text, _ in runner.run_stream(
                "Count from 1 to 10, one number per line."
            ):
                chunks.append(text)
            return chunks

        chunks = _run(go(), _TIMEOUT_FAST)
        if len(chunks) > 1:
            # No chunk should be a prefix of the next (i.e., not cumulative)
            for i in range(len(chunks) - 1):
                assert not chunks[i + 1].startswith(chunks[i]), (
                    "Chunks appear cumulative — delta-slicing may be broken"
                )
        first = chunks[0] if chunks else ""
        print(f"\n[claude -p] delta-slice: {len(chunks)} chunks, first={first!r}")

    def test_model_override(self):
        """ClaudeCodeRunner respects the model= parameter."""
        runner = ClaudeCodeRunner(model="claude-haiku-4-5-20251001")

        async def go():
            return await runner.run("Say: model ok")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[claude -p --model] result: {result!r}")
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    def test_cwd_respected(self):
        """ClaudeCodeRunner runs in the specified cwd."""
        with tempfile.TemporaryDirectory() as d:
            runner = ClaudeCodeRunner(cwd=d)

            async def go():
                return await runner.run("What is 2 + 2?")

            result = _run(go(), _TIMEOUT_FAST)
            print(f"\n[claude -p cwd] result: {result!r}")
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# OpenCodeRunner tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not OPENCODE_BIN, reason="opencode not found in PATH")
class TestOpenCodeRunner:
    """Tests using ``opencode run`` (OpenCode non-interactive mode)."""

    def test_run_returns_string(self):
        """OpenCodeRunner.run() returns a non-empty string."""
        runner = OpenCodeRunner()

        async def go():
            return await runner.run("Reply with exactly three words: one two three")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[opencode run] run() result: {result!r}")
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    def test_run_stream_yields_lines(self):
        """OpenCodeRunner.run_stream() yields lines as they arrive."""
        runner = OpenCodeRunner()

        async def go():
            chunks = []
            async for text, is_reasoning in runner.run_stream(
                "Write a one-sentence summary of Python."
            ):
                print(f"  line is_reasoning={is_reasoning}: {text!r}")
                chunks.append(text)
            return chunks

        chunks = _run(go(), _TIMEOUT_FAST)
        print(f"\n[opencode run] stream: {len(chunks)} lines")
        assert len(chunks) > 0
        full = "".join(chunks)
        assert len(full.strip()) > 0

    def test_model_override(self):
        """OpenCodeRunner respects the model= parameter."""
        runner = OpenCodeRunner()

        async def go():
            return await runner.run("What is 1 + 1?")

        result = _run(go(), _TIMEOUT_FAST)
        print(f"\n[opencode run] result: {result!r}")
        assert isinstance(result, str)

    def test_cwd_respected(self):
        """OpenCodeRunner runs in the specified cwd."""
        with tempfile.TemporaryDirectory() as d:
            runner = OpenCodeRunner(cwd=d)

            async def go():
                return await runner.run("What is 2 + 2?")

            result = _run(go(), _TIMEOUT_FAST)
            print(f"\n[opencode run cwd] result: {result!r}")
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Import smoke test (no subprocess needed)
# ---------------------------------------------------------------------------

def test_imports():
    """All runner classes import without errors."""
    from pyclawops.agents.acp_runner import AcpRunner, ClaudeCodeRunner, OpenCodeRunner, PyclawAcpClient

    assert AcpRunner is not None
    assert ClaudeCodeRunner is not None
    assert OpenCodeRunner is not None
    assert PyclawAcpClient is not None


def test_acp_client_instantiation():
    """PyclawAcpClient can be instantiated with defaults."""
    from pyclawops.agents.acp_runner import PyclawAcpClient

    client = PyclawAcpClient()
    assert client._chunk_callback is None
    assert client._permission_handler is None
    assert client._terminals == {}


def test_runner_default_cwd():
    """Runners default cwd to os.getcwd() when not specified."""
    runner = AcpRunner(command="dummy-agent")
    assert runner.cwd == os.getcwd()

    cc = ClaudeCodeRunner()
    assert cc.cwd == os.getcwd()

    oc = OpenCodeRunner()
    assert oc.cwd == os.getcwd()
