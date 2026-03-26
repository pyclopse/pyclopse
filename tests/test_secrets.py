"""Tests for the secrets manager."""
import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyclopse.secrets.models import (
    EnvSecretDef,
    ExecSecretDef,
    FileSecretDef,
    KeychainSecretDef,
    SecretsConfig,
)
from pyclopse.secrets.manager import SecretsManager, ResolutionError


# ---------------------------------------------------------------------------
# Model parsing
# ---------------------------------------------------------------------------

class TestModels:
    def test_env_def_defaults(self):
        d = EnvSecretDef.model_validate({"source": "env"})
        assert d.source == "env"
        assert d.var is None

    def test_env_def_with_var_override(self):
        d = EnvSecretDef.model_validate({"source": "env", "var": "MY_ENV_VAR"})
        assert d.var == "MY_ENV_VAR"

    def test_file_def_no_id(self):
        d = FileSecretDef.model_validate({"source": "file", "path": "/tmp/secret.txt"})
        assert d.id == ""

    def test_file_def_with_pointer(self):
        d = FileSecretDef.model_validate({"source": "file", "path": "/tmp/s.json", "id": "/key/sub"})
        assert d.id == "/key/sub"

    def test_exec_def_defaults(self):
        d = ExecSecretDef.model_validate({"source": "exec", "command": "/bin/op", "id": "op://X"})
        assert d.json_only is True
        assert d.timeout_ms == 5000
        assert d.allow_symlink_command is False
        assert d.pass_env == []

    def test_exec_def_camel_aliases(self):
        d = ExecSecretDef.model_validate({
            "source": "exec", "command": "/bin/op", "id": "op://X",
            "passEnv": ["HOME"], "jsonOnly": False,
            "timeoutMs": 3000, "allowSymlinkCommand": True,
        })
        assert d.pass_env == ["HOME"]
        assert d.json_only is False
        assert d.timeout_ms == 3000
        assert d.allow_symlink_command is True

    def test_keychain_def_defaults(self):
        d = KeychainSecretDef.model_validate({"source": "keychain", "account": "my-account"})
        assert d.service == "pyclopse"
        assert d.backend == "auto"

    def test_secrets_config_extra_keys(self):
        """SecretsConfig accepts arbitrary secret names as extra fields."""
        cfg = SecretsConfig.model_validate({
            "MY_KEY": {"source": "env"},
            "OTHER": {"source": "keychain", "account": "x"},
        })
        assert cfg.model_extra["MY_KEY"] == {"source": "env"}


# ---------------------------------------------------------------------------
# SecretsManager — env source
# ---------------------------------------------------------------------------

class TestEnvSource:
    def test_resolve_by_registry_name(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-12345")
        mgr = SecretsManager({"MINIMAX_API_KEY": {"source": "env"}})
        assert mgr.resolve_name("MINIMAX_API_KEY") == "sk-12345"

    def test_var_override(self, monkeypatch):
        monkeypatch.setenv("ACTUAL_VAR", "resolved")
        mgr = SecretsManager({"MY_SECRET": {"source": "env", "var": "ACTUAL_VAR"}})
        assert mgr.resolve_name("MY_SECRET") == "resolved"

    def test_missing_env_var(self, monkeypatch):
        monkeypatch.delenv("MISSING_XYZ", raising=False)
        mgr = SecretsManager({"MISSING_XYZ": {"source": "env"}})
        with pytest.raises(ResolutionError, match="not set or empty"):
            mgr.resolve_name("MISSING_XYZ")

    def test_unregistered_name_raises(self):
        mgr = SecretsManager({})
        with pytest.raises(ResolutionError, match="not registered"):
            mgr.resolve_name("NONEXISTENT")

    def test_snapshot_caching(self, monkeypatch):
        monkeypatch.setenv("CACHED_VAR", "first")
        mgr = SecretsManager({"CACHED_VAR": {"source": "env"}})
        val1 = mgr.resolve_name("CACHED_VAR")
        monkeypatch.setenv("CACHED_VAR", "second")
        val2 = mgr.resolve_name("CACHED_VAR")
        assert val1 == val2 == "first"

    def test_reload_clears_snapshot(self, monkeypatch):
        monkeypatch.setenv("RELOAD_VAR", "first")
        mgr = SecretsManager({"RELOAD_VAR": {"source": "env"}})
        mgr.resolve_name("RELOAD_VAR")
        monkeypatch.setenv("RELOAD_VAR", "second")
        mgr.reload()
        assert mgr.resolve_name("RELOAD_VAR") == "second"

    def test_registered_names(self):
        mgr = SecretsManager({
            "A": {"source": "env"},
            "B": {"source": "env", "var": "B_VAR"},
        })
        assert set(mgr.registered_names()) == {"A", "B"}


# ---------------------------------------------------------------------------
# SecretsManager — file source
# ---------------------------------------------------------------------------

class TestFileSource:
    def test_entire_file(self, tmp_path):
        f = tmp_path / "secret.txt"
        f.write_text("my-file-secret\n")
        f.chmod(0o600)
        mgr = SecretsManager({"MY_KEY": {"source": "file", "path": str(f)}})
        assert mgr.resolve_name("MY_KEY") == "my-file-secret"

    def test_json_pointer(self, tmp_path):
        f = tmp_path / "secrets.json"
        f.write_text(json.dumps({"channels": {"telegram": {"botToken": "tg-123"}}}))
        f.chmod(0o600)
        mgr = SecretsManager({"TG_TOKEN": {"source": "file", "path": str(f), "id": "/channels/telegram/botToken"}})
        assert mgr.resolve_name("TG_TOKEN") == "tg-123"

    def test_json_pointer_missing_key(self, tmp_path):
        f = tmp_path / "secrets.json"
        f.write_text(json.dumps({"a": "b"}))
        f.chmod(0o600)
        mgr = SecretsManager({"K": {"source": "file", "path": str(f), "id": "/nonexistent"}})
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_name("K")

    def test_json_pointer_requires_slash(self, tmp_path):
        f = tmp_path / "secrets.json"
        f.write_text(json.dumps({"key": "value"}))
        f.chmod(0o600)
        mgr = SecretsManager({"K": {"source": "file", "path": str(f), "id": "key"}})
        with pytest.raises(ResolutionError, match="JSON pointer"):
            mgr.resolve_name("K")

    def test_json_pointer_tilde_escaping(self, tmp_path):
        f = tmp_path / "secrets.json"
        f.write_text(json.dumps({"a/b": {"c~d": "escaped"}}))
        f.chmod(0o600)
        mgr = SecretsManager({"K": {"source": "file", "path": str(f), "id": "/a~1b/c~0d"}})
        assert mgr.resolve_name("K") == "escaped"

    def test_file_not_found(self):
        mgr = SecretsManager({"K": {"source": "file", "path": "/nonexistent/path.json", "id": "/key"}})
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_name("K")

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        f.chmod(0o600)
        mgr = SecretsManager({"K": {"source": "file", "path": str(f), "id": "/key"}})
        with pytest.raises(ResolutionError, match="not valid JSON"):
            mgr.resolve_name("K")

    def test_non_string_pointer_value(self, tmp_path):
        f = tmp_path / "s.json"
        f.write_text(json.dumps({"nested": {"obj": {"a": 1}}}))
        f.chmod(0o600)
        mgr = SecretsManager({"K": {"source": "file", "path": str(f), "id": "/nested/obj"}})
        with pytest.raises(ResolutionError, match="not a string"):
            mgr.resolve_name("K")


# ---------------------------------------------------------------------------
# SecretsManager — exec source
# ---------------------------------------------------------------------------

class TestExecSource:
    def _make_script(self, tmp_path: Path, content: str) -> Path:
        script = tmp_path / "helper.py"
        script.write_text(f"#!/usr/bin/env python3\n{textwrap.dedent(content)}")
        script.chmod(0o755)
        return script

    def test_simple_mode(self, tmp_path):
        script = self._make_script(tmp_path, """
            import sys
            print(sys.argv[-1] + "-resolved")
        """)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(script), "id": "mykey", "jsonOnly": False}})
        assert mgr.resolve_name("K") == "mykey-resolved"

    def test_json_protocol(self, tmp_path):
        script = self._make_script(tmp_path, """
            import sys, json
            payload = json.load(sys.stdin)
            ids = payload["ids"]
            print(json.dumps({"protocolVersion": 1, "values": {i: i + "-val" for i in ids}}))
        """)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(script), "id": "token123", "jsonOnly": True}})
        assert mgr.resolve_name("K") == "token123-val"

    def test_json_protocol_error_key(self, tmp_path):
        script = self._make_script(tmp_path, """
            import sys, json
            payload = json.load(sys.stdin)
            ids = payload["ids"]
            print(json.dumps({"protocolVersion": 1, "values": {}, "errors": {ids[0]: {"message": "not found"}}}))
        """)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(script), "id": "missing", "jsonOnly": True}})
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_name("K")

    def test_nonzero_exit(self, tmp_path):
        script = self._make_script(tmp_path, """
            import sys
            print("oops", file=sys.stderr)
            sys.exit(1)
        """)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(script), "id": "x", "jsonOnly": False}})
        with pytest.raises(ResolutionError, match="exited 1"):
            mgr.resolve_name("K")

    def test_command_not_found(self):
        mgr = SecretsManager({"K": {"source": "exec", "command": "/nonexistent/binary", "id": "x", "jsonOnly": False}})
        with pytest.raises(ResolutionError, match="command not found"):
            mgr.resolve_name("K")

    def test_symlink_blocked_by_default(self, tmp_path):
        real = tmp_path / "real.py"
        real.write_text("#!/usr/bin/env python3\nprint('hi')")
        real.chmod(0o755)
        link = tmp_path / "link.py"
        link.symlink_to(real)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(link), "id": "x", "jsonOnly": False}})
        with pytest.raises(ResolutionError, match="symlink"):
            mgr.resolve_name("K")

    def test_symlink_allowed_when_configured(self, tmp_path):
        real = tmp_path / "real.py"
        real.write_text("#!/usr/bin/env python3\nprint('resolved')")
        real.chmod(0o755)
        link = tmp_path / "link.py"
        link.symlink_to(real)
        mgr = SecretsManager({"K": {"source": "exec", "command": str(link), "id": "x", "jsonOnly": False, "allowSymlinkCommand": True}})
        assert mgr.resolve_name("K") == "resolved"


# ---------------------------------------------------------------------------
# SecretsManager — keychain source
# ---------------------------------------------------------------------------

class TestKeychainSource:
    def _mgr(self, backend: str = "auto") -> SecretsManager:
        return SecretsManager({
            "MY_SECRET": {"source": "keychain", "account": "myaccount", "service": "pyclopse-test", "backend": backend}
        })

    def test_security_backend_success(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "secret-from-keychain\n"
        mock_result.stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)
        assert self._mgr("security").resolve_name("MY_SECRET") == "secret-from-keychain"

    def test_security_backend_not_found(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 44
        mock_result.stdout = ""
        mock_result.stderr = "The specified item could not be found in the keychain."
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)
        with pytest.raises(ResolutionError, match="Keychain lookup failed"):
            self._mgr("security").resolve_name("MY_SECRET")

    def test_security_binary_missing(self, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(sp, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(ResolutionError, match="security.*command not found"):
            self._mgr("security").resolve_name("MY_SECRET")

    def test_security_timeout(self, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(sp, "run", lambda *a, **kw: (_ for _ in ()).throw(sp.TimeoutExpired("security", 5)))
        with pytest.raises(ResolutionError, match="timed out"):
            self._mgr("security").resolve_name("MY_SECRET")

    def test_keyring_backend_success(self, monkeypatch):
        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = "keyring-secret"
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
        assert self._mgr("keyring").resolve_name("MY_SECRET") == "keyring-secret"
        fake_keyring.get_password.assert_called_once_with("pyclopse-test", "myaccount")

    def test_keyring_entry_missing(self, monkeypatch):
        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = None
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
        with pytest.raises(ResolutionError, match="not found"):
            self._mgr("keyring").resolve_name("MY_SECRET")

    def test_keyring_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "keyring", None)
        with pytest.raises(ResolutionError, match="keyring.*library is required"):
            self._mgr("keyring").resolve_name("MY_SECRET")

    def test_auto_uses_security_on_macos(self, monkeypatch):
        import platform, subprocess as sp
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "auto-mac-secret"
        mock_result.stderr = ""
        calls = []
        monkeypatch.setattr(sp, "run", lambda cmd, **kw: (calls.append(cmd), mock_result)[1])
        assert self._mgr("auto").resolve_name("MY_SECRET") == "auto-mac-secret"
        assert "security" in calls[0]

    def test_auto_uses_keyring_on_linux(self, monkeypatch):
        import platform
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = "linux-secret"
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
        assert self._mgr("auto").resolve_name("MY_SECRET") == "linux-secret"

    def test_unknown_backend_raises(self):
        mgr = SecretsManager({"K": {"source": "keychain", "account": "x", "backend": "nosuchbackend"}})
        with pytest.raises(ResolutionError, match="Unknown keychain backend"):
            mgr.resolve_name("K")


# ---------------------------------------------------------------------------
# SecretsManager — resolve_raw (${NAME} substitution)
# ---------------------------------------------------------------------------

class TestResolveRaw:
    def test_replaces_registered_secret(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tg-12345")
        mgr = SecretsManager({"BOT_TOKEN": {"source": "env"}})
        raw = {"channels": {"telegram": {"botToken": "${BOT_TOKEN}"}}}
        result = mgr.resolve_raw(raw)
        assert result["channels"]["telegram"]["botToken"] == "tg-12345"

    def test_skips_secrets_subtree(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "resolved")
        mgr = SecretsManager({"SOME_VAR": {"source": "env"}})
        raw = {
            "secrets": {"SOME_VAR": {"source": "env"}},
            "other": "${SOME_VAR}",
        }
        result = mgr.resolve_raw(raw)
        assert result["secrets"]["SOME_VAR"] == {"source": "env"}
        assert result["other"] == "resolved"

    def test_walks_list(self, monkeypatch):
        monkeypatch.setenv("K1", "val1")
        monkeypatch.setenv("K2", "val2")
        mgr = SecretsManager({"K1": {"source": "env"}, "K2": {"source": "env"}})
        result = mgr.resolve_raw(["${K1}", "${K2}", "plain"])
        assert result == ["val1", "val2", "plain"]

    def test_passthrough_non_string_scalars(self):
        mgr = SecretsManager({})
        raw = {"num": 42, "flag": True, "nothing": None}
        assert mgr.resolve_raw(raw) == {"num": 42, "flag": True, "nothing": None}

    def test_unregistered_name_returns_original(self):
        mgr = SecretsManager({})
        result = mgr.resolve_raw("${UNREGISTERED_KEY}")
        assert result == "${UNREGISTERED_KEY}"

    def test_non_secret_strings_untouched(self):
        mgr = SecretsManager({})
        assert mgr.resolve_raw("just a string") == "just a string"
        assert mgr.resolve_raw("http://example.com") == "http://example.com"

    def test_resolve_raw_with_file_source(self, tmp_path):
        f = tmp_path / "s.json"
        f.write_text(json.dumps({"token": "file-token-val"}))
        f.chmod(0o600)
        mgr = SecretsManager({"TG_TOKEN": {"source": "file", "path": str(f), "id": "/token"}})
        result = mgr.resolve_raw({"botToken": "${TG_TOKEN}"})
        assert result["botToken"] == "file-token-val"

    def test_resolve_raw_with_keychain_source(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "kc-value"
        mock_result.stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)
        mgr = SecretsManager({"KC_KEY": {"source": "keychain", "account": "my-account", "backend": "security"}})
        result = mgr.resolve_raw({"key": "${KC_KEY}"})
        assert result["key"] == "kc-value"


# ---------------------------------------------------------------------------
# SecretsManager — reload
# ---------------------------------------------------------------------------

class TestReload:
    def test_reload_without_new_registry(self, monkeypatch):
        monkeypatch.setenv("R_VAR", "v1")
        mgr = SecretsManager({"R_VAR": {"source": "env"}})
        mgr.resolve_name("R_VAR")
        monkeypatch.setenv("R_VAR", "v2")
        mgr.reload()
        assert mgr.resolve_name("R_VAR") == "v2"

    def test_reload_with_new_registry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INIT_VAR", "init")
        mgr = SecretsManager({"INIT_VAR": {"source": "env"}})

        f = tmp_path / "s.json"
        f.write_text(json.dumps({"key": "file-value"}))
        f.chmod(0o600)

        mgr.reload({"FILE_KEY": {"source": "file", "path": str(f), "id": "/key"}})
        assert mgr.resolve_name("FILE_KEY") == "file-value"
        with pytest.raises(ResolutionError, match="not registered"):
            mgr.resolve_name("INIT_VAR")


# ---------------------------------------------------------------------------
# Integration: ConfigLoader uses SecretsManager
# ---------------------------------------------------------------------------

class TestConfigLoaderIntegration:
    """ConfigLoader integration tests.

    These tests embed secrets inline in the config YAML (the fallback path).
    We monkeypatch SECRETS_FILE_PATH to a non-existent location so the real
    ~/.pyclopse/secrets/secrets.yaml is not picked up, forcing the fallback.
    """

    @pytest.fixture(autouse=True)
    def no_global_secrets_file(self, tmp_path, monkeypatch):
        """Prevent tests from loading the real ~/.pyclopse/secrets/secrets.yaml."""
        monkeypatch.setattr("pyclopse.config.loader.SECRETS_FILE_PATH", str(tmp_path / "nonexistent.yaml"))

    def test_loader_resolves_secret(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TG_TOKEN", "tg-resolved")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
secrets:
  TG_TOKEN:
    source: env
channels:
  telegram:
    enabled: true
    botToken: "${TG_TOKEN}"
""")
        from pyclopse.config.loader import ConfigLoader
        cfg = ConfigLoader(str(config_file)).load()
        assert cfg.channels.telegram.bot_token == "tg-resolved"

    def test_loader_resolves_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINIMAX_KEY", "sk-mini-123")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
secrets:
  MINIMAX_KEY:
    source: env
providers:
  minimax:
    enabled: true
    apiKey: "${MINIMAX_KEY}"
""")
        from pyclopse.config.loader import ConfigLoader
        cfg = ConfigLoader(str(config_file)).load()
        assert cfg.providers.minimax.api_key == "sk-mini-123"

    def test_loader_resolves_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACTUAL_ENV_VAR", "actual-value")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
secrets:
  MY_SECRET:
    source: env
    var: ACTUAL_ENV_VAR
providers:
  anthropic:
    enabled: true
    apiKey: "${MY_SECRET}"
""")
        from pyclopse.config.loader import ConfigLoader
        cfg = ConfigLoader(str(config_file)).load()
        assert cfg.providers.anthropic.api_key == "actual-value"

    def test_loader_unregistered_ref_left_as_is(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
secrets: {}
providers:
  anthropic:
    enabled: true
    apiKey: "${NOT_REGISTERED}"
""")
        from pyclopse.config.loader import ConfigLoader
        cfg = ConfigLoader(str(config_file)).load()
        # Unresolved reference stays as the original ${...} string
        assert cfg.providers.anthropic.api_key == "${NOT_REGISTERED}"

    def test_loader_reads_dedicated_secrets_file(self, tmp_path, monkeypatch):
        """When secrets.yaml exists next to (or at SECRETS_FILE_PATH), it takes priority."""
        monkeypatch.setenv("DEDICATED_SECRET", "from-secrets-yaml")

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("DEDICATED_SECRET:\n  source: env\n")
        monkeypatch.setattr("pyclopse.config.loader.SECRETS_FILE_PATH", str(secrets_file))

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
providers:
  anthropic:
    enabled: true
    apiKey: "${DEDICATED_SECRET}"
""")
        from pyclopse.config.loader import ConfigLoader
        cfg = ConfigLoader(str(config_file)).load()
        assert cfg.providers.anthropic.api_key == "from-secrets-yaml"
