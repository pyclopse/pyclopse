"""Configuration loader - loads and validates YAML config files."""

import os
from pyclawops.reflect import reflect_system
import yaml
from pathlib import Path
from typing import Optional, Union, Dict, Any
from dotenv import load_dotenv
from .schema import Config
from pyclawops.secrets.manager import SecretsManager

# Load .env file from common locations
for env_path in [
    Path.home() / ".pyclawops" / ".env",
    Path.home() / ".env",
    Path(".env"),
]:
    if env_path.exists():
        load_dotenv(env_path)
        break


# Dedicated secrets registry file — lives outside pyclawops.yaml so it can be
# managed independently (different permissions, separate git-ignore, etc.)
SECRETS_FILE_PATH = "~/.pyclawops/secrets/secrets.yaml"


DEFAULT_CONFIG_PATHS = [
    "~/.pyclawops/config/pyclawops.yaml",
    "~/.pyclawops/config.yaml",
    "~/.pyclawops/config.yml",
    "~/.pyclawops/pyclawops.yaml",
    "./config.yaml",
    "./config.yml",
    "./pyclawops.yaml",
]


def expand_path(path: str) -> Path:
    """Expand ~ and environment variables in a path string.

    Args:
        path (str): Path string potentially containing ``~`` or ``$VAR``
            tokens.

    Returns:
        Path: Fully resolved :class:`pathlib.Path` with all expansions applied.
    """
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_yaml(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Load a YAML file and return its contents as a dictionary.

    Args:
        file_path (Union[str, Path]): Path to the YAML file to read. ``~`` and
            environment variables are expanded.

    Returns:
        Dict[str, Any]: Parsed YAML contents, or an empty dict if the file is
            empty.

    Raises:
        FileNotFoundError: If the file does not exist at the resolved path.
    """
    path = expand_path(str(file_path))
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save_yaml(data: Dict[str, Any], file_path: Union[str, Path]) -> None:
    """Serialise a dictionary to a YAML file, creating parent directories if needed.

    Args:
        data (Dict[str, Any]): Data to serialise.
        file_path (Union[str, Path]): Destination file path. ``~`` and
            environment variables are expanded. Parent directories are created
            automatically.
    """
    path = expand_path(str(file_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def load_secrets_registry(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load the secrets registry.

    Checks ``~/.pyclawops/secrets/secrets.yaml`` first.  If that file does not
    exist, falls back to the ``secrets:`` block inside the main config file
    (for users who have not yet migrated).

    Args:
        config_path: Path to the main pyclawops config file, used only for the
                     fallback lookup.  If omitted, ``find_config_file()`` is
                     called to locate it.
    """
    secrets_path = expand_path(SECRETS_FILE_PATH)
    if secrets_path.exists():
        try:
            return load_yaml(secrets_path)
        except Exception as e:
            import logging
            logging.getLogger("pyclawops.config").warning(
                f"Failed to load secrets file {secrets_path}: {e}"
            )

    # Fallback: secrets: block inside pyclawops.yaml
    cfg_path = Path(str(config_path)) if config_path else find_config_file()
    if cfg_path and cfg_path.exists():
        try:
            return load_yaml(cfg_path).get("secrets", {})
        except Exception:
            pass

    return {}


def find_config_file(search_paths: Optional[list] = None) -> Optional[Path]:
    """Find the first existing config file from a list of candidate paths.

    Args:
        search_paths (Optional[list]): Ordered list of path strings to check.
            Defaults to :data:`DEFAULT_CONFIG_PATHS` when omitted.

    Returns:
        Optional[Path]: The resolved :class:`pathlib.Path` of the first
            existing file found, or ``None`` if none of the candidates exist.
    """
    paths = search_paths or DEFAULT_CONFIG_PATHS
    for path_str in paths:
        path = expand_path(path_str)
        if path.exists():
            return path
    return None


@reflect_system("config")
class ConfigLoader:
    """Loads and manages pyclawops configuration.

    Attributes:
        config_path (Optional[Path]): Resolved path supplied at construction
            time, or ``None`` if no explicit path was given.
    """

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        """Initialise the loader with an optional explicit config path.

        Args:
            config_path (Optional[Union[str, Path]]): Path to the YAML config
                file. ``~`` and environment variables are expanded. When
                omitted, :meth:`load` will search :data:`DEFAULT_CONFIG_PATHS`
                automatically.
        """
        # Convert to Path object for consistency
        self.config_path = expand_path(config_path) if config_path else None
        self._config: Optional[Config] = None

    def load(self, config_path: Optional[Union[str, Path]] = None) -> Config:
        """Load configuration from a YAML file and apply secret resolution.

        Searches :data:`DEFAULT_CONFIG_PATHS` when no path is provided and
        none was given at construction time. Falls back to a default
        :class:`Config` if no file is found anywhere.

        Args:
            config_path (Optional[Union[str, Path]]): Override path for this
                call only. Takes precedence over the constructor path.

        Returns:
            Config: Fully validated and secret-resolved configuration object.
        """
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

        # Resolve secrets before Pydantic validation.
        # Registry is loaded from secrets.yaml (or pyclawops.yaml fallback).
        manager = SecretsManager(load_secrets_registry(path))
        data = manager.resolve_raw(data)

        # Validate with Pydantic
        self._config = Config(**data)
        
        return self._config
    
    def save(self, config_path: Optional[Union[str, Path]] = None) -> None:
        """Serialise the currently loaded configuration to a YAML file.

        Args:
            config_path (Optional[Union[str, Path]]): Destination path for
                this call. Falls back to the constructor path, then to
                ``~/.pyclawops/config.yaml``.

        Raises:
            RuntimeError: If no configuration has been loaded yet.
        """
        if self._config is None:
            raise RuntimeError("No configuration loaded")
        
        path = config_path or self.config_path
        if path is None:
            path = "~/.pyclawops/config.yaml"
        
        # Convert to dict
        data = self._config.model_dump(mode="json")
        
        save_yaml(data, path)
    
    @property
    def config(self) -> Config:
        """Return the currently loaded configuration, loading it first if needed.

        Returns:
            Config: The active :class:`Config` instance.
        """
        if self._config is None:
            self.load()
        return self._config


def create_default_config(path: Union[str, Path] = "~/.pyclawops/config.yaml") -> Config:
    """Create a default config file on disk and return the corresponding Config object.

    Parent directories are created automatically if they do not already exist.

    Args:
        path (Union[str, Path]): Destination path for the new config file.
            Defaults to ``~/.pyclawops/config.yaml``.

    Returns:
        Config: The default :class:`Config` instance that was written to disk.
    """
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
    """Convenience wrapper: create a :class:`ConfigLoader` and load configuration.

    Args:
        config_path (Optional[Union[str, Path]]): Explicit path to the YAML
            config file. When omitted, :data:`DEFAULT_CONFIG_PATHS` are
            searched.

    Returns:
        Config: The loaded and validated :class:`Config` instance.
    """
    loader = ConfigLoader(config_path)
    return loader.load()
