"""Secrets system data models."""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class SecretSource(str, Enum):
    ENV = "env"
    FILE = "file"
    EXEC = "exec"
    KEYCHAIN = "keychain"


class SecretRef(BaseModel):
    """
    A SecretRef is an indirection pointer to a secret value.

    Instead of storing a plaintext credential in config, a SecretRef says
    "look this value up in provider X using id Y at resolution time."

    YAML example::

        api_key:
          source: env
          provider: default
          id: ANTHROPIC_API_KEY

        bot_token:
          source: file
          provider: secrets_file
          id: /channels/telegram/botToken

        signing_secret:
          source: exec
          provider: onepassword
          id: op://Personal/SlackBot/signing_secret
    """
    source: SecretSource
    provider: str = "default"
    id: str

    @classmethod
    def from_dict(cls, d: dict) -> "SecretRef":
        return cls(source=d["source"], provider=d.get("provider", "default"), id=d["id"])

    @classmethod
    def is_ref(cls, value: Any) -> bool:
        """Return True if *value* looks like a SecretRef dict."""
        return (
            isinstance(value, dict)
            and "source" in value
            and "id" in value
            and value.get("source") in ("env", "file", "exec", "keychain")
        )


class EnvProviderConfig(BaseModel):
    """Resolves secrets from environment variables."""
    source: str = "env"
    # Optional allowlist of permitted variable names. Empty = allow all.
    allowlist: List[str] = Field(default_factory=list)


class FileProviderConfig(BaseModel):
    """Resolves secrets from a local JSON file."""
    source: str = "file"
    path: str
    # "json": id is a JSON pointer (/key/subkey); "singleValue": entire file content.
    mode: str = "json"


class ExecProviderConfig(BaseModel):
    """
    Resolves secrets by calling an external binary (1Password CLI, Vault, sops, etc.).

    The binary receives a JSON payload on stdin::

        {"protocolVersion": 1, "provider": "<name>", "ids": ["<id>"]}

    And must write JSON to stdout::

        {"protocolVersion": 1, "values": {"<id>": "<resolved-value>"}}

    When jsonOnly=False the entire stdout is used as the resolved value
    (useful for simple CLIs like ``vault kv get -field=...``).
    """
    source: str = "exec"
    command: str                        # Absolute path to binary
    args: List[str] = Field(default_factory=list)
    pass_env: List[str] = Field(
        default_factory=list,
        validation_alias="passEnv",
    )
    json_only: bool = Field(True, validation_alias="jsonOnly")
    timeout_ms: int = Field(5000, validation_alias="timeoutMs")
    allow_symlink_command: bool = Field(False, validation_alias="allowSymlinkCommand")


class KeychainProviderConfig(BaseModel):
    """
    Resolves secrets from the OS keychain.

    On macOS uses the ``security`` CLI (always available, no extra deps).
    On other platforms falls back to the ``keyring`` library (must be
    installed: ``pip install keyring``).

    The SecretRef ``id`` is the **account name** stored in the keychain
    entry.  The provider's ``service`` field identifies which keychain
    service the entry belongs to.

    YAML example::

        secrets:
          providers:
            keychain:
              source: keychain
              service: pyclaw          # keychain service name (default: "pyclaw")
              backend: auto            # auto | security | keyring (default: auto)
    """
    source: str = "keychain"
    # Keychain service name that groups related entries together.
    service: str = "pyclaw"
    # "auto": use `security` on macOS, `keyring` elsewhere.
    # "security": force macOS `security` CLI.
    # "keyring": force `keyring` library (cross-platform).
    backend: str = "auto"


ProviderConfig = Union[EnvProviderConfig, FileProviderConfig, ExecProviderConfig, KeychainProviderConfig]


class SecretsConfig(BaseModel):
    """
    Top-level secrets configuration block.

    Example pyclaw.yaml::

        secrets:
          providers:
            default:
              source: env
            secrets_file:
              source: file
              path: ~/.pyclaw/secrets.json
            onepassword:
              source: exec
              command: /opt/homebrew/bin/op
              allowSymlinkCommand: true
              args: [read]
              passEnv: [HOME]
              jsonOnly: false
            keychain:
              source: keychain
              service: pyclaw
    """
    providers: Dict[str, Any] = Field(default_factory=dict)
