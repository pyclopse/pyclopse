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

from pyclaw.core.session import Session, SessionManager


logger = logging.getLogger("pyclaw.compaction")


@dataclass
class CompactionConfig:
    """Configuration for compaction behavior."""
    enabled: bool = True
    threshold: int = 165000  # tokens - triggers compaction
    soft_threshold: int = 150000  # tokens - warning threshold
    min_tokens: int = 30000  # minimum tokens to keep after compaction
    max_messages_to_keep: int = 50  # maximum recent messages to keep


@dataclass
class CompactionResult:
    """Result of a compaction operation."""
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
    """
    Manages session compaction to control token usage.
    
    Features:
    - Manual compaction via compact_session()
    - Automatic compaction when threshold is reached
    - Token counting and tracking per session
    """
    
    def __init__(
        self,
        config: Optional[CompactionConfig] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        self.config = config or CompactionConfig()
        self.session_manager = session_manager
        self._token_counts: Dict[str, int] = {}  # session_id -> token count
        self._token_counter: Optional[TokenCounter] = None
        self._summarizer: Optional[Callable[[str, List[Dict[str, str]]], Awaitable[str]]] = None
        self._compaction_task: Optional[asyncio.Task] = None
        self._running = False
    
    def set_token_counter(self, counter: TokenCounter) -> None:
        """Set the function to count tokens in a message list."""
        self._token_counter = counter
    
    def set_summarizer(self, summarizer: Callable[[str, List[Dict[str, str]]], Awaitable[str]]) -> None:
        """Set the function to summarize messages."""
        self._summarizer = summarizer
    
    async def start(self) -> None:
        """Start the compaction manager."""
        self._running = True
        logger.info("Compaction manager started")
    
    async def stop(self) -> None:
        """Stop the compaction manager."""
        self._running = False
        if self._compaction_task:
            self._compaction_task.cancel()
            try:
                await self._compaction_task
            except asyncio.CancelledError:
                pass
        logger.info("Compaction manager stopped")
    
    async def count_tokens(self, session: Session) -> int:
        """Count tokens in a session's messages."""
        if self._token_counter:
            messages = session.get_messages_for_provider()
            return await self._token_counter(messages)
        
        # Fallback: estimate tokens (rough approximation: ~4 chars per token)
        total_chars = sum(
            len(msg.content) 
            for msg in session.messages
        )
        return total_chars // 4
    
    def get_token_count(self, session_id: str) -> int:
        """Get cached token count for a session."""
        return self._token_counts.get(session_id, 0)
    
    async def update_token_count(self, session: Session) -> int:
        """Update and return the token count for a session."""
        count = await self.count_tokens(session)
        self._token_counts[session.id] = count
        return count
    
    def should_compact(self, session: Session) -> bool:
        """Check if a session should be compacted."""
        if not self.config.enabled:
            return False
        
        token_count = self._token_counts.get(session.id, 0)
        return token_count >= self.config.threshold
    
    def needs_warning(self, session: Session) -> bool:
        """Check if session is approaching threshold."""
        token_count = self._token_counts.get(session.id, 0)
        return token_count >= self.config.soft_threshold
    
    async def compact_session(
        self,
        session: Session,
        force: bool = False,
    ) -> CompactionResult:
        """
        Compact a session by summarizing old messages.
        
        Args:
            session: The session to compact
            force: If True, compact regardless of threshold
            
        Returns:
            CompactionResult with details of the operation
        """
        original_count = await self.update_token_count(session)
        
        # Check if compaction is needed
        if not force and original_count < self.config.threshold:
            return CompactionResult(
                success=True,
                original_tokens=original_count,
                compacted_tokens=original_count,
                messages_summarized=0,
                messages_kept=len(session.messages),
                summary="No compaction needed - below threshold",
            )
        
        if not self._summarizer:
            return CompactionResult(
                success=False,
                original_tokens=original_count,
                compacted_tokens=original_count,
                messages_summarized=0,
                messages_kept=len(session.messages),
                summary="",
                error="No summarizer configured",
            )
        
        try:
            # Separate messages into to-summarize and to-keep
            messages_to_summarize: List[Dict[str, str]] = []
            messages_to_keep: List[Any] = []
            
            # Keep recent messages up to max_messages_to_keep
            keep_count = min(
                self.config.max_messages_to_keep,
                len(session.messages)
            )
            
            # System messages should always be kept at the start
            system_messages = [m for m in session.messages if m.role == "system"]
            non_system = [m for m in session.messages if m.role != "system"]
            
            # Keep system messages + recent non-system messages
            keep_messages = system_messages + non_system[-keep_count:]
            
            # Messages to summarize are everything else
            summarize_messages = non_system[:-keep_count] if len(non_system) > keep_count else []
            
            if not summarize_messages:
                return CompactionResult(
                    success=True,
                    original_tokens=original_count,
                    compacted_tokens=original_count,
                    messages_summarized=0,
                    messages_kept=len(session.messages),
                    summary="No messages to summarize",
                )
            
            # Convert to provider format for summarizer
            summarize_data = [
                {"role": msg.role, "content": msg.content}
                for msg in summarize_messages
            ]
            
            # Generate summary
            logger.info(
                f"Compacting session {session.id}: "
                f"summarizing {len(summarize_messages)} messages"
            )
            
            summary = await self._summarizer(
                "Summarize this conversation concisely, preserving key facts, "
                "decisions, and important context:",
                summarize_data
            )
            
            # Create summary message
            from pyclaw.core.session import Message
            summary_message = Message(
                id=f"summary_{session.id}_{datetime.utcnow().timestamp()}",
                role="system",
                content=f"[Previous conversation summarized]\n\n{summary}",
                metadata={"is_summary": True, "original_count": len(summarize_messages)},
            )
            
            # Replace messages with summary + recent messages
            session.messages = [summary_message] + keep_messages
            
            # Update token count
            new_count = await self.update_token_count(session)
            
            logger.info(
                f"Compaction complete for session {session.id}: "
                f"{original_count} -> {new_count} tokens"
            )
            
            return CompactionResult(
                success=True,
                original_tokens=original_count,
                compacted_tokens=new_count,
                messages_summarized=len(summarize_messages),
                messages_kept=len(session.messages),
                summary=summary[:500] + "..." if len(summary) > 500 else summary,
            )
            
        except Exception as e:
            logger.error(f"Compaction failed for session {session.id}: {e}")
            return CompactionResult(
                success=False,
                original_tokens=original_count,
                compacted_tokens=original_count,
                messages_summarized=0,
                messages_kept=len(session.messages),
                summary="",
                error=str(e),
            )
    
    async def check_and_compact(self, session: Session) -> Optional[CompactionResult]:
        """
        Check if compaction is needed and perform it.
        
        Returns:
            CompactionResult if compaction was performed, None otherwise
        """
        if self.should_compact(session):
            return await self.compact_session(session)
        return None
    
    def get_status(self, session: Session) -> Dict[str, Any]:
        """Get compaction status for a session."""
        token_count = self._token_counts.get(session.id, 0)
        return {
            "enabled": self.config.enabled,
            "token_count": token_count,
            "threshold": self.config.threshold,
            "soft_threshold": self.config.soft_threshold,
            "should_compact": self.should_compact(session),
            "needs_warning": self.needs_warning(session),
            "message_count": len(session.messages),
        }
    
    @classmethod
    def from_config(cls, config_dict: Dict[str, Any], **kwargs) -> "CompactionManager":
        """Create CompactionManager from config dict."""
        compaction_config = CompactionConfig(
            enabled=config_dict.get("enabled", True),
            threshold=config_dict.get("threshold", 165000),
            soft_threshold=config_dict.get("soft_threshold", 150000),
            min_tokens=config_dict.get("min_tokens", 30000),
        )
        return cls(config=compaction_config, **kwargs)


# Default token counter using tiktoken (if available)
async def default_token_counter(messages: List[Dict[str, str]]) -> int:
    """Count tokens using tiktoken if available, otherwise estimate."""
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
    """
    Default summarizer that combines messages.
    
    Note: In production, this should use an LLM to generate summaries.
    This is a simple placeholder.
    """
    # Format messages for summarization
    formatted = "\n".join(
        f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
        for msg in messages
    )
    
    # Return combined (in production, call an LLM here)
    return f"[{len(messages)} messages summarized - {len(formatted)} characters]"
