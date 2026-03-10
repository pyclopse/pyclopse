"""
Tests for <thinking> tag stripping in AgentRunner.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyclaw.agents.runner import strip_thinking_tags


# ---------------------------------------------------------------------------
# strip_thinking_tags utility
# ---------------------------------------------------------------------------

class TestStripThinkingTags:

    def test_no_thinking_tags_unchanged(self):
        text = "Hello, how are you?"
        assert strip_thinking_tags(text) == text

    def test_single_thinking_block_removed(self):
        text = "<thinking>I need to think about this.</thinking>Here is my answer."
        result = strip_thinking_tags(text)
        assert "thinking" not in result.lower()
        assert "Here is my answer." in result

    def test_multiline_thinking_block_removed(self):
        text = "<thinking>\nLet me reason step by step.\nOk, I think the answer is 42.\n</thinking>\nThe answer is 42."
        result = strip_thinking_tags(text)
        assert "<thinking>" not in result
        assert "The answer is 42." in result

    def test_multiple_thinking_blocks_all_removed(self):
        text = "<thinking>First thought.</thinking>Result A<thinking>Second thought.</thinking>Result B"
        result = strip_thinking_tags(text)
        assert "<thinking>" not in result
        assert "Result A" in result
        assert "Result B" in result

    def test_case_insensitive(self):
        text = "<THINKING>hidden</THINKING>visible"
        result = strip_thinking_tags(text)
        assert "hidden" not in result
        assert "visible" in result

    def test_empty_thinking_block_removed(self):
        text = "<thinking></thinking>response"
        result = strip_thinking_tags(text)
        assert "<thinking>" not in result
        assert "response" in result

    def test_only_thinking_block_returns_empty(self):
        text = "<thinking>Just thinking.</thinking>"
        result = strip_thinking_tags(text)
        assert result == ""

    def test_excess_newlines_collapsed(self):
        text = "<thinking>think</thinking>\n\n\n\nAnswer here."
        result = strip_thinking_tags(text)
        assert "\n\n\n" not in result
        assert "Answer here." in result

    def test_no_false_positives_on_similar_tags(self):
        text = "<thought>Keep this</thought>and this"
        result = strip_thinking_tags(text)
        assert "<thought>Keep this</thought>" in result


# ---------------------------------------------------------------------------
# AgentRunner.run() with show_thinking=False (default)
# ---------------------------------------------------------------------------

class TestAgentRunnerThinkingStrip:

    def _make_runner(self, show_thinking=False):
        from pyclaw.agents.runner import AgentRunner
        runner = AgentRunner(
            agent_name="test",
            instruction="You are helpful.",
            model="sonnet",
            show_thinking=show_thinking,
        )
        return runner

    @pytest.mark.asyncio
    async def test_thinking_stripped_by_default(self):
        runner = self._make_runner(show_thinking=False)
        mock_app = MagicMock()
        mock_app.send = AsyncMock(
            return_value="<thinking>internal reasoning</thinking>Final answer."
        )
        runner._app = mock_app

        with patch("pyclaw.core.concurrency.get_manager") as mock_mgr:
            mock_mgr.return_value.acquire.return_value.__aenter__ = AsyncMock(return_value=None)
            mock_mgr.return_value.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await runner.run("question")

        assert "internal reasoning" not in result
        assert "Final answer." in result

    @pytest.mark.asyncio
    async def test_thinking_preserved_when_show_thinking_true(self):
        runner = self._make_runner(show_thinking=True)
        mock_app = MagicMock()
        mock_app.send = AsyncMock(
            return_value="<thinking>internal reasoning</thinking>Final answer."
        )
        runner._app = mock_app

        with patch("pyclaw.core.concurrency.get_manager") as mock_mgr:
            mock_mgr.return_value.acquire.return_value.__aenter__ = AsyncMock(return_value=None)
            mock_mgr.return_value.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await runner.run("question")

        assert "<thinking>internal reasoning</thinking>" in result

    @pytest.mark.asyncio
    async def test_response_without_thinking_unchanged(self):
        runner = self._make_runner(show_thinking=False)
        mock_app = MagicMock()
        mock_app.send = AsyncMock(return_value="Plain response with no tags.")
        runner._app = mock_app

        with patch("pyclaw.core.concurrency.get_manager") as mock_mgr:
            mock_mgr.return_value.acquire.return_value.__aenter__ = AsyncMock(return_value=None)
            mock_mgr.return_value.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await runner.run("question")

        assert result == "Plain response with no tags."

    def test_show_thinking_default_is_false(self):
        from pyclaw.agents.runner import AgentRunner
        runner = AgentRunner("test", "instruction")
        assert runner.show_thinking is False
