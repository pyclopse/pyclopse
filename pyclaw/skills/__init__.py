"""Skills system for pyclaw."""

import asyncio
import functools
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypeVar, Awaitable

from pyclaw.providers import Message


# Type for skill functions
SkillFunc = TypeVar("SkillFunc", bound=Callable[..., Awaitable[Any]])


@dataclass
class Skill:
    """Definition of a skill."""
    name: str
    description: str
    func: Callable
    parameters: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    
    def to_openai_tool(self) -> Dict[str, Any]:
        """Convert skill to OpenAI tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": [
                        k for k, v in self.parameters.items()
                        if v.get("required", False)
                    ],
                },
            },
        }


class SkillRegistry:
    """Registry for managing skills."""
    
    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._logger = logging.getLogger("pyclaw.skills")
    
    def register(
        self,
        name: str,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> Callable[[SkillFunc], SkillFunc]:
        """Decorator to register a skill."""
        def decorator(func: SkillFunc) -> SkillFunc:
            skill = Skill(
                name=name,
                description=description or func.__doc__ or "",
                func=func,
                parameters=parameters or {},
                tags=tags or [],
            )
            self._skills[name] = skill
            
            # Also register with lowercase name for convenience
            self._skills[name.lower()] = skill
            
            self._logger.debug(f"Registered skill: {name}")
            
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)
            
            return wrapper
        
        return decorator
    
    def register_skill(self, skill: Skill) -> None:
        """Register a skill directly."""
        self._skills[skill.name] = skill
        self._skills[skill.name.lower()] = skill
    
    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._skills.get(name)
    
    def list_skills(self) -> List[Skill]:
        """List all registered skills."""
        # Return unique skills (avoid duplicates from lowercase aliases)
        seen = set()
        unique = []
        for skill in self._skills.values():
            if skill.name not in seen:
                seen.add(skill.name)
                unique.append(skill)
        return unique
    
    def list_by_tag(self, tag: str) -> List[Skill]:
        """List skills by tag."""
        return [s for s in self.list_skills() if tag in s.tags]
    
    def get_tools(self) -> List[Dict[str, Any]]:
        """Get all skills as OpenAI tools."""
        return [skill.to_openai_tool() for skill in self.list_skills()]
    
    def remove(self, name: str) -> bool:
        """Remove a skill by name."""
        skill = self._skills.pop(name, None)
        if skill:
            self._skills.pop(skill.name.lower(), None)
            return True
        return False
    
    def clear(self) -> None:
        """Clear all skills."""
        self._skills.clear()


# Global registry instance
_registry = SkillRegistry()


def get_registry() -> SkillRegistry:
    """Get the global skill registry."""
    return _registry


# Convenience decorator
def skill(
    name: str,
    description: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
) -> Callable[[SkillFunc], SkillFunc]:
    """Decorator to register a skill with the global registry."""
    return _registry.register(name, description, parameters, tags)


# Export public types
__all__ = [
    "Skill",
    "SkillRegistry",
    "get_registry",
    "skill",
]
