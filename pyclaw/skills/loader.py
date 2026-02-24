"""Skill loader for loading skills from the filesystem."""

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import Skill, SkillRegistry


class SkillLoader:
    """Load skills from the filesystem."""
    
    def __init__(
        self,
        registry: Optional[SkillRegistry] = None,
        skills_dirs: Optional[List[Path]] = None,
    ):
        self._registry = registry
        self._skills_dirs = skills_dirs or []
        self._logger = logging.getLogger("pyclaw.skills.loader")
        self._loaded_modules: Set[Path] = set()
    
    @property
    def registry(self) -> SkillRegistry:
        """Get the skill registry."""
        if self._registry is None:
            from . import get_registry
            self._registry = get_registry()
        return self._registry
    
    def add_skills_dir(self, path: Path) -> None:
        """Add a directory to search for skills."""
        if path not in self._skills_dirs:
            self._skills_dirs.append(path)
    
    def discover_skills(self) -> Dict[str, Path]:
        """Discover all skills in the skills directories."""
        skills = {}
        
        for skills_dir in self._skills_dirs:
            if not skills_dir.exists():
                self._logger.warning(f"Skills directory does not exist: {skills_dir}")
                continue
            
            # Look for Python files that define skills
            for entry in skills_dir.iterdir():
                if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
                    skills[entry.stem] = entry
                elif entry.is_dir():
                    # Check for __init__.py or skill.py
                    init_file = entry / "__init__.py"
                    skill_file = entry / "skill.py"
                    
                    if init_file.exists():
                        skills[entry.name] = init_file
                    elif skill_file.exists():
                        skills[entry.name] = skill_file
        
        return skills
    
    def load_skill_file(self, path: Path) -> None:
        """Load skills from a single Python file."""
        if path in self._loaded_modules:
            self._logger.debug(f"Already loaded: {path}")
            return
        
        module_name = f"pyclaw.skills.{path.stem}"
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._loaded_modules.add(path)
                self._logger.info(f"Loaded skills from: {path}")
        except Exception as e:
            self._logger.error(f"Failed to load skills from {path}: {e}")
    
    def load_all(self) -> int:
        """Load all discovered skills."""
        skills = self.discover_skills()
        count = 0
        
        for name, path in skills.items():
            self.load_skill_file(path)
            count += 1
        
        self._logger.info(f"Loaded {count} skill file(s)")
        return count
    
    def reload_skill(self, name: str) -> bool:
        """Reload a specific skill by name."""
        skills = self.discover_skills()
        
        if name not in skills:
            self._logger.warning(f"Skill not found: {name}")
            return False
        
        path = skills[name]
        
        # Remove from loaded set to force reload
        self._loaded_modules.discard(path)
        
        # Reload
        self.load_skill_file(path)
        return True
    
    def unload_skill(self, name: str) -> bool:
        """Unload a specific skill."""
        skill = self.registry.get(name)
        
        if skill:
            self.registry.remove(name)
            self._logger.info(f"Unloaded skill: {name}")
            return True
        
        return False


def create_default_loader(
    base_path: Optional[Path] = None,
    registry: Optional[SkillRegistry] = None,
) -> SkillLoader:
    """Create a default skill loader with common paths."""
    if base_path is None:
        # Default to ~/.pyclaw/skills or ./skills
        base_path = Path.home() / ".pyclaw" / "skills"
    
    skills_dirs = [
        base_path,
        Path(__file__).parent / "builtin",  # Built-in skills
    ]
    
    return SkillLoader(registry=registry, skills_dirs=skills_dirs)


# Built-in skill loader that auto-discovers and loads
_default_loader: Optional[SkillLoader] = None


def get_default_loader() -> SkillLoader:
    """Get the default skill loader."""
    global _default_loader
    if _default_loader is None:
        _default_loader = create_default_loader()
    return _default_loader


def load_builtin_skills() -> None:
    """Load built-in skills."""
    loader = get_default_loader()
    loader.load_all()


__all__ = [
    "SkillLoader",
    "create_default_loader",
    "get_default_loader",
    "load_builtin_skills",
]
