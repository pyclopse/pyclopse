"""Skill runner for executing skills."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from . import Skill, SkillRegistry


@dataclass
class SkillContext:
    """Context for skill execution."""
    agent_id: str
    session_id: str
    message_id: Optional[str] = None
    channel: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    """Result from skill execution."""
    skill_name: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)


class SkillRunner:
    """Execute skills with proper context and error handling."""
    
    def __init__(
        self,
        registry: Optional[SkillRegistry] = None,
        allowed_skills: Optional[Set[str]] = None,
        blocked_skills: Optional[Set[str]] = None,
    ):
        self._registry = registry
        self._allowed_skills = allowed_skills
        self._blocked_skills = blocked_skills or set()
        self._logger = logging.getLogger("pyclaw.skills.runner")
        self._execution_history: List[SkillResult] = []
    
    @property
    def registry(self) -> SkillRegistry:
        """Get the skill registry."""
        if self._registry is None:
            from . import get_registry
            self._registry = get_registry()
        return self._registry
    
    def is_allowed(self, skill_name: str) -> bool:
        """Check if a skill is allowed to run."""
        if skill_name in self._blocked_skills:
            return False
        
        if self._allowed_skills is not None:
            return skill_name in self._allowed_skills
        
        return True
    
    async def execute(
        self,
        skill_name: str,
        args: Optional[Dict[str, Any]] = None,
        context: Optional[SkillContext] = None,
    ) -> SkillResult:
        """Execute a skill by name."""
        start_time = datetime.utcnow()
        
        # Get the skill
        skill = self.registry.get(skill_name)
        
        if not skill:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Skill not found: {skill_name}",
                duration_ms=0.0,
            )
        
        # Check if allowed
        if not self.is_allowed(skill_name):
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Skill not allowed: {skill_name}",
                duration_ms=0.0,
            )
        
        # Execute the skill
        try:
            args = args or {}
            
            # Add context to args if provided
            if context:
                args["_context"] = context
            
            # Check if the skill is a coroutine function
            if asyncio.iscoroutinefunction(skill.func):
                result = await skill.func(**args)
            else:
                # Run sync functions in a thread pool
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: skill.func(**args))
            
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            skill_result = SkillResult(
                skill_name=skill_name,
                success=True,
                result=result,
                duration_ms=duration_ms,
            )
            
            self._execution_history.append(skill_result)
            self._logger.debug(f"Executed skill {skill_name} in {duration_ms:.2f}ms")
            
            return skill_result
            
        except Exception as e:
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            skill_result = SkillResult(
                skill_name=skill_name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )
            
            self._execution_history.append(skill_result)
            self._logger.error(f"Skill {skill_name} failed: {e}")
            
            return skill_result
    
    async def execute_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        context: Optional[SkillContext] = None,
    ) -> Dict[str, Any]:
        """Execute a tool call from the model."""
        result = await self.execute(tool_name, arguments, context)
        
        return {
            "tool_call_id": tool_call_id,
            "output": result.result if result.success else None,
            "error": result.error if not result.success else None,
            "is_error": not result.success,
        }
    
    def get_execution_history(
        self,
        skill_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[SkillResult]:
        """Get execution history."""
        history = self._execution_history
        
        if skill_name:
            history = [h for h in history if h.skill_name == skill_name]
        
        return history[-limit:]
    
    def clear_history(self) -> None:
        """Clear execution history."""
        self._execution_history.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics."""
        if not self._execution_history:
            return {
                "total_executions": 0,
                "successful": 0,
                "failed": 0,
                "success_rate": 0.0,
                "avg_duration_ms": 0.0,
            }
        
        total = len(self._execution_history)
        successful = sum(1 for h in self._execution_history if h.success)
        failed = total - successful
        avg_duration = sum(h.duration_ms for h in self._execution_history) / total
        
        return {
            "total_executions": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0.0,
            "avg_duration_ms": avg_duration,
        }


# Default runner instance
_default_runner: Optional[SkillRunner] = None


def get_default_runner() -> SkillRunner:
    """Get the default skill runner."""
    global _default_runner
    if _default_runner is None:
        _default_runner = SkillRunner()
    return _default_runner


__all__ = [
    "SkillContext",
    "SkillResult",
    "SkillRunner",
    "get_default_runner",
]
