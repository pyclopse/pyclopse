"""Read, merge, and write pyclopse config + secrets files."""

from pathlib import Path
from typing import Any
import yaml


def load_existing(data_dir: Path) -> tuple[dict, dict, dict]:
    """Load existing config, secrets, and env from data_dir.

    Returns:
        (config_data, secrets_data, env_data) — all dicts, empty if not found.
    """
    config_path = data_dir / "config" / "pyclopse.yaml"
    secrets_path = data_dir / "secrets" / "secrets.yaml"
    env_path = data_dir / ".env"

    config_data: dict = {}
    secrets_data: dict = {}
    env_data: dict = {}

    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}

    if secrets_path.exists():
        with open(secrets_path) as f:
            secrets_data = yaml.safe_load(f) or {}

    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_data[k.strip()] = v.strip()

    return config_data, secrets_data, env_data


def write_all(data_dir: Path, config: dict, secrets: dict, env: dict) -> None:
    """Write config.yaml, secrets.yaml, and .env to data_dir."""
    config_path = data_dir / "config" / "pyclopse.yaml"
    secrets_path = data_dir / "secrets" / "secrets.yaml"
    env_path = data_dir / ".env"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    with open(secrets_path, "w") as f:
        yaml.dump(secrets, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    lines = ["# pyclopse secrets — do not commit this file"]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def config_path(data_dir: Path) -> Path:
    return data_dir / "config" / "pyclopse.yaml"


def has_config(data_dir: Path) -> bool:
    return config_path(data_dir).exists()
