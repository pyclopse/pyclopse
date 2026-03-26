"""
Session compaction for managing token usage.

This module provides:
- Manual compaction via /compact command
- Automatic compaction when token threshold is reached
- Message summarization to reduce token count while preserving context
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable, Awaitable

from pyclopse.core.session import Session, SessionManager


logger = logging.getLogger("pyclopse.compaction")


@dataclass
class CompactionConfig:
    """Configuration for session compaction behavior.

    Attributes:
        enabled (bool): Whether automatic compaction is active. Defaults to True.
        threshold (int): Token count that triggers compaction. Defaults to 165000.
        soft_threshold (int): Token count that triggers a warning. Defaults to 150000.
        min_tokens (int): Minimum tokens to retain after compaction. Defaults to 30000.
        max_messages_to_keep (int): Maximum recent messages to keep after compaction.
            Defaults to 50.
    """

    enabled: bool = True
    threshold: int = 165000  # tokens - triggers compaction
    soft_threshold: int = 150000  # tokens - warning threshold
    min_tokens: int = 30000  # minimum tokens to keep after compaction
    max_messages_to_keep: int = 50  # maximum recent messages to keep


@dataclass
class CompactionResult:
    """Result of a compaction operation.

    Attributes:
        success (bool): Whether the compaction completed successfully.
        original_tokens (int): Token count before compaction.
        compacted_tokens (int): Token count after compaction.
        messages_summarized (int): Number of messages replaced by a summary.
        messages_kept (int): Number of recent messages preserved verbatim.
        summary (str): Human-readable description of what was done.
        error (Optional[str]): Error message if compaction failed. Defaults to None.
    """

    success: bool
    original_tokens: int
    compacted_tokens: int
    messages_summarized: int
    messages_kept: int
    summary: str
    error: Optional[str] = None


# Token counter function type
TokenCounter = Callable[[List[Dict[str, str]]], Awaitable[int]]


class CompactionManager:
    """Manages session compaction to control token usage.

    Provides manual compaction (via compact_session()), automatic threshold
    checking (via check_and_compact()), and per-session token count tracking.
    Compaction is pluggable via set_token_counter() and set_summarizer().

    Attributes:
        config (CompactionConfig): Compaction thresholds and settings.
        session_manager (Optional[SessionManager]): The session manager instance.
    """

    def __init__(
        self,
        config: Optional[CompactionConfig] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        """Initialize the CompactionManager.

        Args:
            config (Optional[CompactionConfig]): Compaction settings. Defaults to
                CompactionConfig() with standard thresholds.
            session_manager (Optional[SessionManager]): Session manager used for
                session lookups. Defaults to None.
        """
        self.config = config or CompactionConfig()
        self.session_manager = session_manager
        self._token_counts: Dict[str, int] = {}  # session_id -> token count
        self._token_counter: Optional[TokenCounter] = None
        self._summarizer: Optional[Callable[[str, List[Dict[str, str]]], Awaitable[str]]] = None
        self._compaction_task: Optional[asyncio.Task] = None
        self._running = False

    def set_token_counter(self, counter: TokenCounter) -> None:
        """Set the async function used to count tokens in a message list.

        Args:
            counter (TokenCounter): Async callable that accepts a list of
                ``{"role": str, "content": str}`` dicts and returns an int
                token count.
        """
        self._token_counter = counter

    def set_summarizer(self, summarizer: Callable[[str, List[Dict[str, str]]], Awaitable[str]]) -> None:
        """Set the async function used to summarize a message list.

        Args:
            summarizer (Callable[[str, List[Dict[str, str]]], Awaitable[str]]): Async
                callable that accepts an instruction string and a list of message
                dicts, and returns a summary string.
        """
        self._summarizer = summarizer

    async def start(self) -> None:
        """Start the compaction manager."""
        self._running = True
        logger.info("Compaction manager started")

    async def stop(self) -> None:
        """Stop the compaction manager and cancel any in-flight compaction task."""
        self._running = False
        if self._compaction_task:
            self._compaction_task.cancel()
            try:
                await self._compaction_task
            except asyncio.CancelledError:
                pass
        logger.info("Compaction manager stopped")

    async def count_tokens(self, session: Session) -> int:
        """Estimate the token count for a session's conversation history.

        If a token counter is configured and the session has a history file,
        loads the file and counts tokens precisely.  Otherwise falls back to a
        rough estimate of 500 tokens per message exchange.

        Args:
            session (Session): The session whose history should be counted.

        Returns:
            int: Estimated token count.
        """
        if self._token_counter:
            # Load history from disk to count tokens
            if session.history_path and session.history_path.exists():
                try:
                    from fast_agent.mcp.prompt_serialization import load_messages
                    messages = load_messages(str(session.history_path))
                    msg_dicts = [
                        {"role": m.role, "content": m.all_text() or ""}
                        for m in messages
                    ]
                    return await self._token_counter(msg_dicts)
                except Exception:
                    pass
        # Fallback: rough estimate (~500 tokens per user/assistant exchange)
        return session.message_count * 500

    def get_token_count(self, session_id: str) -> int:
        """Get the cached token count for a session.

        Returns 0 if update_token_count() has not been called for this session.

        Args:
            session_id (str): Session identifier.

        Returns:
            int: Last known token count, or 0 if uncached.
        """
        return self._token_counts.get(session_id, 0)

    async def update_token_count(self, session: Session) -> int:
        """Recount tokens for a session and update the cache.

        Args:
            session (Session): The session to update.

        Returns:
            int: The freshly computed token count.
        """
        count = await self.count_tokens(session)
        self._token_counts[session.id] = count
        return count

    def should_compact(self, session: Session) -> bool:
        """Check if a session has exceeded the compaction threshold.

        Args:
            session (Session): The session to check.

        Returns:
            bool: True if compaction is enabled and the cached token count is at
                or above the configured threshold.
        """
        if not self.config.enabled:
            return False

        token_count = self._token_counts.get(session.id, 0)
        return token_count >= self.config.threshold

    def needs_warning(self, session: Session) -> bool:
        """Check if a session is approaching the compaction threshold.

        Args:
            session (Session): The session to check.

        Returns:
            bool: True if the cached token count is at or above the soft threshold.
        """
        token_count = self._token_counts.get(session.id, 0)
        return token_count >= self.config.soft_threshold

    async def compact_session(
        self,
        session: Session,
        force: bool = False,
    ) -> CompactionResult:
        """Compact a session by summarizing old messages.

        Currently returns a failure result directing the user to use /reset,
        as full file-based history compaction is not yet implemented.

        Args:
            session (Session): The session to compact.
            force (bool): If True, compact regardless of the threshold check.
                Defaults to False.

        Returns:
            CompactionResult: Details of the operation, including success flag,
                token counts, and any error message.
        """
        original_count = await self.update_token_count(session)

        # Check if compaction is needed
        if not force and original_count < self.config.threshold:
            return CompactionResult(
                success=True,
                original_tokens=original_count,
                compacted_tokens=original_count,
                messages_summarized=0,
                messages_kept=session.message_count,
                summary="No compaction needed - below threshold",
            )

        # Compaction requires loading history from disk, summarizing, and rewriting.
        # This is a placeholder — full compaction support requires runner access.
        return CompactionResult(
            success=False,
            original_tokens=original_count,
            compacted_tokens=original_count,
            messages_summarized=0,
            messages_kept=session.message_count,
            summary="",
            error="Compaction not yet supported with file-based history; use /reset to clear history.",
        )

    async def check_and_compact(self, session: Session) -> Optional[CompactionResult]:
        """Check if compaction is needed and perform it if so.

        Args:
            session (Session): The session to evaluate.

        Returns:
            Optional[CompactionResult]: The compaction result if compaction was
                performed; None if the session was below the threshold.
        """
        if self.should_compact(session):
            return await self.compact_session(session)
        return None

    def get_status(self, session: Session) -> Dict[str, Any]:
        """Get compaction status for a session.

        Args:
            session (Session): The session to query.

        Returns:
            Dict[str, Any]: Status dict with keys: ``enabled``, ``token_count``,
                ``threshold``, ``soft_threshold``, ``should_compact``,
                ``needs_warning``, ``message_count``.
        """
        token_count = self._token_counts.get(session.id, 0)
        return {
            "enabled": self.config.enabled,
            "token_count": token_count,
            "threshold": self.config.threshold,
            "soft_threshold": self.config.soft_threshold,
            "should_compact": self.should_compact(session),
            "needs_warning": self.needs_warning(session),
            "message_count": session.message_count,
        }

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any], **kwargs) -> "CompactionManager":
        """Create a CompactionManager from a configuration dictionary.

        Args:
            config_dict (Dict[str, Any]): Dict with optional keys: ``enabled``,
                ``threshold``, ``soft_threshold``, ``min_tokens``.
            **kwargs: Additional keyword arguments forwarded to the constructor.

        Returns:
            CompactionManager: A new manager instance configured from the dict.
        """
        compaction_config = CompactionConfig(
            enabled=config_dict.get("enabled", True),
            threshold=config_dict.get("threshold", 165000),
            soft_threshold=config_dict.get("soft_threshold", 150000),
            min_tokens=config_dict.get("min_tokens", 30000),
        )
        return cls(config=compaction_config, **kwargs)


# Default token counter using tiktoken (if available)
async def default_token_counter(messages: List[Dict[str, str]]) -> int:
    """Count tokens using tiktoken if available, otherwise estimate.

    Uses the ``cl100k_base`` encoding (GPT-4/3.5 compatible) when tiktoken is
    installed.  Falls back to a character-based estimate (~4 chars per token).

    Args:
        messages (List[Dict[str, str]]): List of message dicts with ``role``
            and ``content`` keys.

    Returns:
        int: Estimated total token count across all messages.
    """
    try:
        import tiktoken
        
        # Use cl100k_base for GPT-4/3.5
        encoder = tiktoken.get_encoding("cl100k_base")
        
        total = 0
        for msg in messages:
            # Count content
            total += encoder.encode(msg.get("content", ""))
            # Add overhead for role
            total += encoder.encode(msg.get("role", ""))
        
        return total
        
    except ImportError:
        # Fallback: rough estimate (~4 chars per token)
        total_chars = sum(len(msg.get("content", "")) for msg in messages)
        return total_chars // 4


# Default summarizer using a simple approach
async def default_summarizer(
    instruction: str,
    messages: List[Dict[str, str]],
) -> str:
    """Default summarizer that produces a placeholder summary of messages.

    This is a simple stub; production use should replace it with an LLM call
    via set_summarizer() on the CompactionManager.

    Args:
        instruction (str): Summarization instruction (e.g. "summarize this
            conversation for resumption").
        messages (List[Dict[str, str]]): List of message dicts with ``role``
            and ``content`` keys to summarize.

    Returns:
        str: A brief placeholder describing how many messages were processed.
    """
    # Format messages for summarization
    formatted = "\n".join(
        f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
        for msg in messages
    )
    
    # Return combined (in production, call an LLM here)
    return f"[{len(messages)} messages summarized - {len(formatted)} characters]"
