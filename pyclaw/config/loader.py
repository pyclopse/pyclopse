"""Configuration loader - loads and validates YAML config files."""

import os
import yaml
from pathlib import Path
from typing import Optional, Union, Dict, Any
from .schema import Config


DEFAULT_CONFIG_PATHS = [
    "~/.pyclaw/config/pyclaw.yaml",
    "~/.pyclaw/config.yaml",
    "~/.pyclaw/config.yml",
    "~/.pyclaw/pyclaw.yaml",
    "./config.yaml",
    "./config.yml",
    "./pyclaw.yaml",
]


def expand_path(path: str) -> Path:
    """Expand ~ and environment variables in path."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_yaml(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Load YAML file and return as dict."""
    path = expand_path(str(file_path))
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save_yaml(data: Dict[str, Any], file_path: Union[str, Path]) -> None:
    """Save dict as YAML file."""
    path = expand_path(str(file_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def find_config_file(search_paths: Optional[list] = None) -> Optional[Path]:
    """Find config file in search paths."""
    paths = search_paths or DEFAULT_CONFIG_PATHS
    for path_str in paths:
        path = expand_path(path_str)
        if path.exists():
            return path
    return None


class ConfigLoader:
    """Loads and manages pyclaw configuration."""
    
    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        self.config_path = config_path
        self._config: Optional[Config] = None
    
    def load(self, config_path: Optional[Union[str, Path]] = None) -> Config:
        """Load configuration from YAML file."""
        path = config_path or self.config_path
        
        if path is None:
            # Try to find config file
            found_path = find_config_file()
            if found_path is None:
                # Return default config
                self._config = Config()
                return self._config
            path = found_path
        
        # Load YAML
        data = load_yaml(path)
        
        # Validate with Pydantic
        self._config = Config(**data)
        
        return self._config
    
    def save(self, config_path: Optional[Union[str, Path]] = None) -> None:
        """Save current configuration to YAML file."""
        if self._config is None:
            raise RuntimeError("No configuration loaded")
        
        path = config_path or self.config_path
        if path is None:
            path = "~/.pyclaw/config.yaml"
        
        # Convert to dict
        data = self._config.model_dump(mode="json")
        
        save_yaml(data, path)
    
    @property
    def config(self) -> Config:
        """Get current configuration."""
        if self._config is None:
            self.load()
        return self._config


def create_default_config(path: Union[str, Path] = "~/.pyclaw/config.yaml") -> Config:
    """Create a default config file and return Config object."""
    config = Config()
    
    # Ensure directory exists
    config_path = expand_path(str(path))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save default config
    data = config.model_dump(mode="json")
    save_yaml(data, path)
    
    return config


# Convenience function
def load_config(config_path: Optional[Union[str, Path]] = None) -> Config:
    """Load configuration from file."""
    loader = ConfigLoader(config_path)
    return loader.load()
