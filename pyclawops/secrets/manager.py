"""SecretsManager — registry-based secret resolution.

Secrets are registered by name in the config under ``secrets:``.
Resolution happens eagerly at startup (and on reload).  Runtime code reads
from the snapshot; secret-provider outages never hit hot request paths.

Resolution is synchronous (config loading is sync).  The exec source
uses subprocess.run with a timeout.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    EnvSecretDef,
    ExecSecretDef,
    FileSecretDef,
    KeychainSecretDef,
)

logger = logging.getLogger("pyclawops.secrets")


class ResolutionError(Exception):
    """Raised when a named secret cannot be resolved."""


class SecretsManager:
    """
    Resolves named secrets from the registry defined in ``secrets:`` config.

    Typical usage — called by ConfigLoader before Pydantic validation::

        manager = SecretsManager(raw_config.get("secrets", {}))
        resolved_data = manager.resolve_raw(raw_config)
        config = Config(**resolved_data)

    In config YAML, reference any registered secret by name::

        providers:
          minimax:
            api_key: "${MINIMAX_API_KEY}"
    """

    def __init__(self, registry: Optional[Dict[str, Any]] = None) -> None:
        self._raw_registry: Dict[str, Any] = registry or {}
        # Resolved snapshot: secret name → plaintext value
        self._snapshot: Dict[str, str] = {}
        self._parsed: Dict[str, Any] = {}
        self._parse_registry()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def resolve_raw(self, data: Any) -> Any:
        """
        Walk *data* (raw YAML dict/list) and replace every ``${NAME}``
        string with its resolved secret value.

        The ``secrets`` subtree itself is never walked — secret definitions
        are not subject to substitution.
        """
        return self._walk(data)

    def resolve_name(self, name: str) -> str:
        """Resolve a named secret from the registry and return its plaintext value."""
        if name in self._snapshot:
            return self._snapshot[name]

        if name not in self._parsed:
            raise ResolutionError(
                f"Secret '{name}' is not registered. "
                "Add it under 'secrets:' in your config."
            )

        value = self._resolve_defn(name, self._parsed[name])
        self._snapshot[name] = value
        return value

    def reload(self, registry: Optional[Dict[str, Any]] = None) -> None:
        """
        Clear the snapshot so the next resolve_name / resolve_raw call
        re-fetches all values.

        If *registry* is provided, the registry is replaced first.
        """
        if registry is not None:
            self._raw_registry = registry
            self._parse_registry()
        self._snapshot.clear()
        logger.info("Secrets snapshot cleared — will re-resolve on next access")

    def registered_names(self) -> List[str]:
        """Return the list of registered secret names."""
        return list(self._parsed.keys())

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _parse_registry(self) -> None:
        """Parse raw registry dicts into typed definition objects."""
        self._parsed = {}
        for name, raw in self._raw_registry.items():
            if not isinstance(raw, dict):
                logger.warning(f"Secret '{name}' definition is not a dict — skipping")
                continue
            source = raw.get("source", "env")
            try:
                if source == "env":
                    self._parsed[name] = EnvSecretDef.model_validate(raw)
                elif source == "file":
                    self._parsed[name] = FileSecretDef.model_validate(raw)
                elif source == "exec":
                    self._parsed[name] = ExecSecretDef.model_validate(raw)
                elif source == "keychain":
                    self._parsed[name] = KeychainSecretDef.model_validate(raw)
                else:
                    logger.warning(f"Unknown source '{source}' for secret '{name}'")
            except Exception as exc:
                logger.warning(f"Failed to parse secret '{name}': {exc}")

    def _walk(self, node: Any) -> Any:
        """Recursively replace ``${NAME}`` strings with resolved values."""
        if isinstance(node, dict):
            # Skip the secrets subtree — never resolve inside secret definitions
            return {k: (v if k == "secrets" else self._walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [self._walk(item) for item in node]
        if isinstance(node, str):
            return self._resolve_string(node)
        return node

    def _resolve_string(self, value: str) -> str:
        """Replace ``${NAME}`` with the resolved secret value."""
        if not (value.startswith("${") and value.endswith("}")):
            return value
        name = value[2:-1]
        try:
            return self.resolve_name(name)
        except ResolutionError as exc:
            logger.error(f"Secret reference '${{{name}}}' could not be resolved: {exc}")
            return value  # return original string on failure

    def _resolve_defn(self, name: str, defn: Any) -> str:
        """Dispatch resolution to the correct source handler."""
        if isinstance(defn, EnvSecretDef):
            return self._resolve_env(defn, name)
        if isinstance(defn, FileSecretDef):
            return self._resolve_file(defn)
        if isinstance(defn, ExecSecretDef):
            return self._resolve_exec(defn)
        if isinstance(defn, KeychainSecretDef):
            return self._resolve_keychain(defn)
        raise ResolutionError(f"Unknown definition type for secret '{name}'")

    # ------------------------------------------------------------------ #
    # Source resolvers
    # ------------------------------------------------------------------ #

    def _resolve_env(self, defn: EnvSecretDef, registry_name: str) -> str:
        var_name = defn.var or registry_name
        value = os.environ.get(var_name)
        if not value:
            raise ResolutionError(
                f"Environment variable '{var_name}' is not set or empty"
            )
        return value

    def _resolve_file(self, defn: FileSecretDef) -> str:
        path = Path(defn.path).expanduser()
        if not path.exists():
            raise ResolutionError(f"Secrets file not found: {path}")

        mode = path.stat().st_mode & 0o777
        if mode & 0o044:
            logger.warning(
                f"Secrets file {path} has permissive permissions ({oct(mode)}). "
                "Consider: chmod 600 " + str(path)
            )

        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ResolutionError(f"Cannot read secrets file {path}: {exc}")

        # No id → entire file is the secret value
        if not defn.id:
            return content

        # id present → treat as JSON pointer
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ResolutionError(f"Secrets file {path} is not valid JSON: {exc}")

        return self._json_pointer(data, defn.id, path)

    def _json_pointer(self, data: dict, pointer: str, path: Path) -> str:
        """Resolve a JSON Pointer (RFC 6901) against *data*."""
        if not pointer.startswith("/"):
            raise ResolutionError(
                f"Secret id '{pointer}' must be a JSON pointer starting with '/' "
                "(e.g. /channels/telegram/botToken)"
            )
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
        node = data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise ResolutionError(f"JSON pointer '{pointer}' not found in {path}")
            node = node[part]
        if not isinstance(node, str):
            raise ResolutionError(
                f"JSON pointer '{pointer}' in {path} resolves to "
                f"{type(node).__name__}, not a string"
            )
        return node

    def _resolve_exec(self, defn: ExecSecretDef) -> str:
        cmd_path = Path(defn.command)

        if not defn.allow_symlink_command and cmd_path.is_symlink():
            raise ResolutionError(
                f"Exec command '{defn.command}' is a symlink. "
                "Set allowSymlinkCommand: true to permit this."
            )
        if not cmd_path.exists():
            raise ResolutionError(f"Exec command not found: {defn.command}")

        if defn.json_only:
            cmd = [defn.command] + defn.args
            stdin_payload = json.dumps({
                "protocolVersion": 1,
                "provider": "pyclawops",
                "ids": [defn.id],
            })
        else:
            cmd = [defn.command] + defn.args + [defn.id]
            stdin_payload = None

        env = {var: os.environ[var] for var in defn.pass_env if var in os.environ}
        timeout_s = defn.timeout_ms / 1000

        try:
            result = subprocess.run(
                cmd,
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env if env else None,
            )
        except subprocess.TimeoutExpired:
            raise ResolutionError(
                f"Exec command '{defn.command}' timed out after {defn.timeout_ms}ms"
            )
        except OSError as exc:
            raise ResolutionError(f"Failed to run exec command '{defn.command}': {exc}")

        if result.returncode != 0:
            raise ResolutionError(
                f"Exec command '{defn.command}' exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise ResolutionError(f"Exec command '{defn.command}' produced no output")

        if not defn.json_only:
            return stdout

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ResolutionError(
                f"Exec command '{defn.command}' returned invalid JSON: {exc}"
            )

        errors = response.get("errors", {})
        if defn.id in errors:
            raise ResolutionError(
                f"Exec command error for '{defn.id}': "
                f"{errors[defn.id].get('message', errors[defn.id])}"
            )

        values = response.get("values", {})
        if defn.id not in values:
            raise ResolutionError(
                f"Exec command '{defn.command}' response missing key '{defn.id}'"
            )

        return str(values[defn.id])

    def _resolve_keychain(self, defn: KeychainSecretDef) -> str:
        import platform
        backend = defn.backend
        if backend == "auto":
            backend = "security" if platform.system() == "Darwin" else "keyring"

        if backend == "security":
            return self._keychain_via_security(defn.service, defn.account)
        if backend == "keyring":
            return self._keychain_via_keyring(defn.service, defn.account)
        raise ResolutionError(
            f"Unknown keychain backend '{backend}'. Use 'auto', 'security', or 'keyring'."
        )

    def _keychain_via_security(self, service: str, account: str) -> str:
        cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            raise ResolutionError(
                "macOS 'security' command not found. "
                "Use backend: keyring for cross-platform support."
            )
        except subprocess.TimeoutExpired:
            raise ResolutionError("Keychain lookup timed out")

        if result.returncode != 0:
            raise ResolutionError(
                f"Keychain lookup failed for service='{service}' account='{account}': "
                f"{result.stderr.strip()}"
            )

        value = result.stdout.strip()
        if not value:
            raise ResolutionError(
                f"Keychain entry service='{service}' account='{account}' is empty"
            )
        return value

    def _keychain_via_keyring(self, service: str, account: str) -> str:
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError:
            raise ResolutionError(
                "The 'keyring' library is required for keychain support on non-macOS platforms. "
                "Install it with: pip install keyring"
            )

        value = keyring.get_password(service, account)
        if value is None:
            raise ResolutionError(
                f"Keychain entry not found: service='{service}' account='{account}'"
            )
        return value
