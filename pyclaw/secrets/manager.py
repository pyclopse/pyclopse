"""
SecretsManager — resolves SecretRefs into an in-memory snapshot.

Resolution happens eagerly at startup (and on reload).  Runtime code reads
from the snapshot; secret-provider outages never hit hot request paths.

Resolution is synchronous (config loading is sync).  The exec provider
uses subprocess.run with a timeout.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    EnvProviderConfig,
    ExecProviderConfig,
    FileProviderConfig,
    KeychainProviderConfig,
    SecretRef,
    SecretsConfig,
)

logger = logging.getLogger("pyclaw.secrets")


class ResolutionError(Exception):
    """Raised when a SecretRef cannot be resolved."""


class SecretsManager:
    """
    Resolves SecretRefs and maintains an in-memory snapshot.

    Typical usage — called by ConfigLoader before Pydantic validation::

        manager = SecretsManager(raw_config.get("secrets", {}))
        resolved_data = manager.resolve_raw(raw_config)
        config = Config(**resolved_data)
    """

    def __init__(self, secrets_cfg: Optional[Dict[str, Any]] = None) -> None:
        self._cfg = SecretsConfig(**(secrets_cfg or {}))
        # Resolved snapshot: "provider:id" → plaintext value
        self._snapshot: Dict[str, str] = {}
        self._parsed_providers: Dict[str, Any] = {}
        self._parse_providers()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def resolve_raw(self, data: Any) -> Any:
        """
        Walk *data* (raw YAML dict/list) and replace every SecretRef dict
        with its resolved string value.  Returns a new structure; does not
        mutate *data*.

        Also handles the legacy ``${VAR_NAME}`` env-var syntax for backwards
        compatibility (but that's now done here rather than in each
        field_validator).
        """
        return self._walk(data)

    def resolve_ref(self, ref: SecretRef) -> str:
        """Resolve a single SecretRef and return the plaintext value."""
        cache_key = f"{ref.provider}:{ref.id}"
        if cache_key in self._snapshot:
            return self._snapshot[cache_key]

        value = self._resolve_one(ref)
        self._snapshot[cache_key] = value
        return value

    def reload(self, secrets_cfg: Optional[Dict[str, Any]] = None) -> None:
        """
        Re-parse provider config and clear the snapshot so the next
        resolve_ref / resolve_raw call re-fetches all values.

        If *secrets_cfg* is provided, the provider config is updated first.
        """
        if secrets_cfg is not None:
            self._cfg = SecretsConfig(**(secrets_cfg or {}))
            self._parse_providers()
        self._snapshot.clear()
        logger.info("Secrets snapshot cleared — will re-resolve on next access")

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _parse_providers(self) -> None:
        """Parse provider config dicts into typed objects."""
        self._parsed_providers = {}
        for name, raw in self._cfg.providers.items():
            if not isinstance(raw, dict):
                continue
            source = raw.get("source", "env")
            try:
                if source == "env":
                    self._parsed_providers[name] = EnvProviderConfig(**raw)
                elif source == "file":
                    self._parsed_providers[name] = FileProviderConfig(**raw)
                elif source == "exec":
                    self._parsed_providers[name] = ExecProviderConfig(**raw)
                elif source == "keychain":
                    self._parsed_providers[name] = KeychainProviderConfig(**raw)
                else:
                    logger.warning(f"Unknown secrets provider source '{source}' for '{name}'")
            except Exception as exc:
                logger.warning(f"Failed to parse secrets provider '{name}': {exc}")

    def _walk(self, node: Any) -> Any:
        """Recursively replace SecretRefs and ${VAR} strings."""
        if isinstance(node, dict):
            if SecretRef.is_ref(node):
                # This dict IS a SecretRef — resolve it
                try:
                    ref = SecretRef.from_dict(node)
                    return self.resolve_ref(ref)
                except ResolutionError as exc:
                    logger.error(f"SecretRef resolution failed: {exc}")
                    return None
            # Otherwise recurse into dict values (skip the secrets.providers
            # subtree to avoid resolving provider config values themselves)
            return {k: (v if k == "secrets" else self._walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [self._walk(item) for item in node]
        if isinstance(node, str):
            return self._resolve_env_syntax(node)
        return node

    def _resolve_env_syntax(self, value: str) -> str:
        """
        Resolve ``${...}`` inline secret references.

        Supported syntaxes::

            ${VAR_NAME}              # env var — backwards-compatible shorthand
            ${env:VAR_NAME}          # env var — explicit source prefix
            ${keychain:Account Name} # OS keychain, default service=pyclaw
            ${file:~/.path/to/file}  # file singleValue read
            ${provider_name:id}      # named provider from secrets.providers

        Resolution order when a colon is present:
          1. Built-in source names: env, keychain, file
          2. Configured provider names (from secrets.providers)
          3. Warn and return the original value unchanged.
        """
        if not (value.startswith("${") and value.endswith("}")):
            return value

        inner = value[2:-1]

        # No colon — bare env var (legacy behaviour)
        if ":" not in inner:
            resolved = os.environ.get(inner)
            if resolved is None:
                logger.warning(f"Environment variable '{inner}' not set")
            return resolved or value

        source, _, ref_id = inner.partition(":")

        # --- built-in: env ---
        if source == "env":
            resolved = os.environ.get(ref_id)
            if resolved is None:
                logger.warning(f"Environment variable '{ref_id}' not set")
            return resolved or value

        # --- built-in: keychain ---
        if source == "keychain":
            # Use configured keychain provider if available, else default
            provider = self._parsed_providers.get("keychain")
            if provider is None:
                provider = KeychainProviderConfig()  # service=pyclaw, backend=auto
            try:
                return self._resolve_keychain(provider, ref_id)
            except ResolutionError as exc:
                logger.error(f"Inline keychain lookup failed for '{ref_id}': {exc}")
                return value

        # --- built-in: file (singleValue) ---
        if source == "file":
            path = Path(ref_id).expanduser()
            if not path.exists():
                logger.error(f"Inline file secret not found: {path}")
                return value
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.error(f"Inline file secret read failed '{path}': {exc}")
                return value

        # --- named provider fallback ---
        named_provider = self._parsed_providers.get(source)
        if named_provider is not None:
            try:
                # Build a minimal SecretRef that routes to this named provider
                src_str = getattr(named_provider, "source", "env")
                ref = SecretRef.from_dict({"source": src_str, "provider": source, "id": ref_id})
                return self.resolve_ref(ref)
            except ResolutionError as exc:
                logger.error(f"Inline provider '{source}' failed for '{ref_id}': {exc}")
                return value

        logger.warning(
            f"Unknown inline secret source '{source}' in '{value}'. "
            "Expected: env, keychain, file, or a configured provider name."
        )
        return value

    def _resolve_one(self, ref: SecretRef) -> str:
        """Dispatch to the correct resolver based on source type."""
        provider_name = ref.provider
        provider = self._parsed_providers.get(provider_name)

        if provider is None:
            # Fall back to implicit env provider if not configured
            if provider_name == "default":
                return self._resolve_env(None, ref.id)
            raise ResolutionError(
                f"Secrets provider '{provider_name}' not configured. "
                f"Add it under secrets.providers in your config."
            )

        if isinstance(provider, EnvProviderConfig):
            return self._resolve_env(provider, ref.id)
        if isinstance(provider, FileProviderConfig):
            return self._resolve_file(provider, ref.id)
        if isinstance(provider, ExecProviderConfig):
            return self._resolve_exec(provider, ref.id)
        if isinstance(provider, KeychainProviderConfig):
            return self._resolve_keychain(provider, ref.id)

        raise ResolutionError(f"Unknown provider type for '{provider_name}'")

    # ------------------------------------------------------------------ #
    # Source resolvers
    # ------------------------------------------------------------------ #

    def _resolve_env(self, provider: Optional[EnvProviderConfig], var_name: str) -> str:
        if provider and provider.allowlist and var_name not in provider.allowlist:
            raise ResolutionError(
                f"Environment variable '{var_name}' not in provider allowlist"
            )
        value = os.environ.get(var_name)
        if not value:
            raise ResolutionError(
                f"Environment variable '{var_name}' is not set or empty"
            )
        return value

    def _resolve_file(self, provider: FileProviderConfig, ref_id: str) -> str:
        path = Path(provider.path).expanduser()
        if not path.exists():
            raise ResolutionError(f"Secrets file not found: {path}")

        # Security: ensure file is not world-readable
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

        if provider.mode == "singleValue":
            return content

        # JSON pointer mode
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ResolutionError(f"Secrets file {path} is not valid JSON: {exc}")

        return self._json_pointer(data, ref_id, path)

    def _json_pointer(self, data: dict, pointer: str, path: Path) -> str:
        """Resolve a JSON Pointer (RFC 6901) against *data*."""
        if not pointer.startswith("/"):
            raise ResolutionError(
                f"SecretRef id '{pointer}' for file provider must be a "
                "JSON pointer starting with '/' (e.g. /channels/telegram/botToken)"
            )
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
        node = data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise ResolutionError(
                    f"JSON pointer '{pointer}' not found in {path}"
                )
            node = node[part]
        if not isinstance(node, str):
            raise ResolutionError(
                f"JSON pointer '{pointer}' in {path} resolves to a "
                f"{type(node).__name__}, not a string"
            )
        return node

    def _resolve_exec(self, provider: ExecProviderConfig, ref_id: str) -> str:
        cmd_path = Path(provider.command)

        if not provider.allow_symlink_command and cmd_path.is_symlink():
            raise ResolutionError(
                f"Exec provider command '{provider.command}' is a symlink. "
                "Set allowSymlinkCommand: true to permit this."
            )
        if not cmd_path.exists():
            raise ResolutionError(
                f"Exec provider command not found: {provider.command}"
            )

        # Build command: binary + configured args + ref_id (appended if jsonOnly=False)
        if provider.json_only:
            cmd = [provider.command] + provider.args
            stdin_payload = json.dumps({
                "protocolVersion": 1,
                "provider": "pyclaw",
                "ids": [ref_id],
            })
        else:
            # Simple mode: append id as final arg, no stdin protocol
            cmd = [provider.command] + provider.args + [ref_id]
            stdin_payload = None

        # Build environment
        env = {}
        for var in provider.pass_env:
            if var in os.environ:
                env[var] = os.environ[var]

        timeout_s = provider.timeout_ms / 1000
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
                f"Exec provider '{provider.command}' timed out after {provider.timeout_ms}ms"
            )
        except OSError as exc:
            raise ResolutionError(
                f"Failed to run exec provider '{provider.command}': {exc}"
            )

        if result.returncode != 0:
            raise ResolutionError(
                f"Exec provider '{provider.command}' exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise ResolutionError(
                f"Exec provider '{provider.command}' produced no output"
            )

        if not provider.json_only:
            return stdout

        # Parse JSON protocol response
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ResolutionError(
                f"Exec provider '{provider.command}' returned invalid JSON: {exc}"
            )

        errors = response.get("errors", {})
        if ref_id in errors:
            raise ResolutionError(
                f"Exec provider error for '{ref_id}': {errors[ref_id].get('message', errors[ref_id])}"
            )

        values = response.get("values", {})
        if ref_id not in values:
            raise ResolutionError(
                f"Exec provider '{provider.command}' response missing key '{ref_id}'"
            )

        return str(values[ref_id])

    def _resolve_keychain(self, provider: KeychainProviderConfig, account: str) -> str:
        """
        Resolve a secret from the OS keychain.

        Backend selection (``provider.backend``):
          - ``"auto"``     — ``security`` CLI on macOS, ``keyring`` library elsewhere.
          - ``"security"`` — macOS ``security find-generic-password`` CLI.
          - ``"keyring"``  — cross-platform ``keyring`` library.
        """
        import platform
        backend = provider.backend
        if backend == "auto":
            backend = "security" if platform.system() == "Darwin" else "keyring"

        if backend == "security":
            return self._keychain_via_security(provider.service, account)
        if backend == "keyring":
            return self._keychain_via_keyring(provider.service, account)
        raise ResolutionError(
            f"Unknown keychain backend '{backend}'. Use 'auto', 'security', or 'keyring'."
        )

    def _keychain_via_security(self, service: str, account: str) -> str:
        """Use the macOS ``security`` CLI to look up a keychain entry."""
        cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            raise ResolutionError(
                "macOS 'security' command not found. "
                "Use backend: keyring for cross-platform support."
            )
        except subprocess.TimeoutExpired:
            raise ResolutionError("Keychain lookup timed out")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise ResolutionError(
                f"Keychain lookup failed for service='{service}' account='{account}': {stderr}"
            )

        value = result.stdout.strip()
        if not value:
            raise ResolutionError(
                f"Keychain entry service='{service}' account='{account}' is empty"
            )
        return value

    def _keychain_via_keyring(self, service: str, account: str) -> str:
        """Use the ``keyring`` library (cross-platform) to look up a keychain entry."""
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
