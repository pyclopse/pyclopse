"""Secrets system data models."""

from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class EnvSecretDef(BaseModel):
    """Read a secret from an environment variable.

    The env var name defaults to the registry key (the secret name itself).
    Set ``var`` to override when the env var name differs from the secret name.

    YAML example::

        secrets:
          MINIMAX_API_KEY:
            source: env
            # reads env var MINIMAX_API_KEY
          OPENAI_KEY:
            source: env
            var: OPENAI_API_KEY   # reads env var OPENAI_API_KEY, registered as OPENAI_KEY
    """
    source: Literal["env"] = "env"
    var: Optional[str] = None  # env var name; defaults to the registry key at resolution time


class FileSecretDef(BaseModel):
    """Read a secret from a file.

    Omit ``id`` (or leave empty) to read the whole file as a single string value.
    Set ``id`` to a JSON pointer (starting with ``/``) to extract a key from a JSON file.

    YAML example::

        secrets:
          DB_PASSWORD:
            source: file
            path: ~/.pyclawops/secrets/db.txt       # reads entire file
          TG_BOT_TOKEN:
            source: file
            path: ~/.pyclawops/secrets/tokens.json
            id: /channels/telegram/botToken      # JSON pointer
    """
    source: Literal["file"] = "file"
    path: str
    # JSON pointer (e.g. /channels/telegram/botToken).
    # Empty = read entire file as a single value.
    id: str = ""


class ExecSecretDef(BaseModel):
    """Read a secret by running an external command (1Password CLI, Vault, sops, etc.).

    When ``json_only=True`` (default): sends a JSON payload on stdin and expects
    a JSON response on stdout::

        stdin:  {"protocolVersion": 1, "provider": "pyclawops", "ids": ["<id>"]}
        stdout: {"protocolVersion": 1, "values": {"<id>": "<value>"}}

    When ``json_only=False``: ``id`` is appended as the final CLI argument and
    the entire stdout is used as the secret value (useful for simple CLIs).

    YAML example::

        secrets:
          OP_SECRET:
            source: exec
            command: /opt/homebrew/bin/op
            id: op://Personal/Bot/token
            args: [read]
            passEnv: [HOME]
            jsonOnly: false
            allowSymlinkCommand: true
    """
    source: Literal["exec"] = "exec"
    command: str
    id: str
    args: List[str] = Field(default_factory=list)
    pass_env: List[str] = Field(default_factory=list, validation_alias="passEnv")
    json_only: bool = Field(True, validation_alias="jsonOnly")
    timeout_ms: int = Field(5000, validation_alias="timeoutMs")
    allow_symlink_command: bool = Field(False, validation_alias="allowSymlinkCommand")


class KeychainSecretDef(BaseModel):
    """Read a secret from the OS keychain.

    On macOS uses the ``security`` CLI (no extra dependencies).
    On other platforms uses the ``keyring`` library (``pip install keyring``).

    YAML example::

        secrets:
          SLACK_BOT_TOKEN:
            source: keychain
            account: pyclawops-slack-bot   # keychain account name
            service: pyclawops             # optional; defaults to "pyclawops"
    """
    source: Literal["keychain"] = "keychain"
    account: str  # keychain account name
    service: str = "pyclawops"
    backend: str = "auto"  # auto | security | keyring


class SecretsConfig(BaseModel):
    """Flat registry of named secrets.

    Every key is a secret name; every value is a source definition.
    Reference any registered secret anywhere in config with ``${NAME}``.

    YAML example::

        secrets:
          MINIMAX_API_KEY:
            source: env
          TG_BOT_TOKEN:
            source: keychain
            account: pyclawops-telegram-bot
          TRADING_KEY:
            source: file
            path: ~/.pyclawops/secrets/trading.json
            id: /trading/api_key
    """
    model_config = ConfigDict(extra="allow")
