"""Tests for the secrets manager."""
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pyclaw.secrets.models import (
    SecretRef,
    SecretSource,
    EnvProviderConfig,
    FileProviderConfig,
    ExecProviderConfig,
    SecretsConfig,
)
from pyclaw.secrets.manager import SecretsManager, ResolutionError


# ---------------------------------------------------------------------------
# SecretRef.is_ref
# ---------------------------------------------------------------------------

class TestSecretRefIsRef:
    def test_env_ref(self):
        assert SecretRef.is_ref({"source": "env", "id": "MY_VAR"})

    def test_file_ref(self):
        assert SecretRef.is_ref({"source": "file", "id": "/key/sub"})

    def test_exec_ref(self):
        assert SecretRef.is_ref({"source": "exec", "id": "op://Personal/Bot/token"})

    def test_missing_source(self):
        assert not SecretRef.is_ref({"id": "MY_VAR"})

    def test_missing_id(self):
        assert not SecretRef.is_ref({"source": "env"})

    def test_unknown_source(self):
        assert not SecretRef.is_ref({"source": "vault", "id": "some/path"})

    def test_not_a_dict(self):
        assert not SecretRef.is_ref("plain string")
        assert not SecretRef.is_ref(42)
        assert not SecretRef.is_ref(None)


# ---------------------------------------------------------------------------
# SecretsManager — env provider
# ---------------------------------------------------------------------------

class TestEnvProvider:
    def test_resolve_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "supersecret")
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="default", id="MY_SECRET")
        assert mgr.resolve_ref(ref) == "supersecret"

    def test_missing_env_var(self):
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="default", id="NONEXISTENT_VAR_XYZ")
        with pytest.raises(ResolutionError, match="not set or empty"):
            mgr.resolve_ref(ref)

    def test_allowlist_permits(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_VAR", "value123")
        mgr = SecretsManager({
            "providers": {
                "myenv": {"source": "env", "allowlist": ["ALLOWED_VAR"]}
            }
        })
        ref = SecretRef(source=SecretSource.ENV, provider="myenv", id="ALLOWED_VAR")
        assert mgr.resolve_ref(ref) == "value123"

    def test_allowlist_blocks(self, monkeypatch):
        monkeypatch.setenv("SECRET_VAR", "nope")
        mgr = SecretsManager({
            "providers": {
                "myenv": {"source": "env", "allowlist": ["ALLOWED_VAR"]}
            }
        })
        ref = SecretRef(source=SecretSource.ENV, provider="myenv", id="SECRET_VAR")
        with pytest.raises(ResolutionError, match="not in provider allowlist"):
            mgr.resolve_ref(ref)

    def test_snapshot_caching(self, monkeypatch):
        monkeypatch.setenv("CACHED_VAR", "first_value")
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="default", id="CACHED_VAR")
        val1 = mgr.resolve_ref(ref)
        monkeypatch.setenv("CACHED_VAR", "second_value")
        val2 = mgr.resolve_ref(ref)
        # Should return cached value
        assert val1 == val2 == "first_value"

    def test_reload_clears_snapshot(self, monkeypatch):
        monkeypatch.setenv("RELOAD_VAR", "first")
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="default", id="RELOAD_VAR")
        mgr.resolve_ref(ref)
        monkeypatch.setenv("RELOAD_VAR", "second")
        mgr.reload()
        assert mgr.resolve_ref(ref) == "second"

    def test_unknown_provider_raises(self):
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="noprovider", id="X")
        with pytest.raises(ResolutionError, match="not configured"):
            mgr.resolve_ref(ref)


# ---------------------------------------------------------------------------
# SecretsManager — file provider
# ---------------------------------------------------------------------------

class TestFileProvider:
    def test_single_value_mode(self, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("my-file-secret\n")
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "singleValue"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="unused")
        assert mgr.resolve_ref(ref) == "my-file-secret"

    def test_json_pointer(self, tmp_path):
        secret_file = tmp_path / "secrets.json"
        data = {"channels": {"telegram": {"botToken": "tg-bot-123"}}}
        secret_file.write_text(json.dumps(data))
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/channels/telegram/botToken")
        assert mgr.resolve_ref(ref) == "tg-bot-123"

    def test_json_pointer_missing_key(self, tmp_path):
        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps({"a": "b"}))
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/nonexistent")
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_ref(ref)

    def test_json_pointer_requires_slash_prefix(self, tmp_path):
        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps({"key": "value"}))
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="key")
        with pytest.raises(ResolutionError, match="JSON pointer"):
            mgr.resolve_ref(ref)

    def test_json_pointer_tilde_escaping(self, tmp_path):
        secret_file = tmp_path / "secrets.json"
        data = {"a/b": {"c~d": "escaped-value"}}
        secret_file.write_text(json.dumps(data))
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        # /a~1b = key "a/b", /c~0d = key "c~d"
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/a~1b/c~0d")
        assert mgr.resolve_ref(ref) == "escaped-value"

    def test_file_not_found(self):
        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": "/nonexistent/path/secrets.json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/key")
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_ref(ref)

    def test_invalid_json(self, tmp_path):
        secret_file = tmp_path / "bad.json"
        secret_file.write_text("not json {{{")
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/key")
        with pytest.raises(ResolutionError, match="not valid JSON"):
            mgr.resolve_ref(ref)

    def test_non_string_pointer_value(self, tmp_path):
        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps({"nested": {"obj": {"a": 1}}}))
        secret_file.chmod(0o600)

        mgr = SecretsManager({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/nested/obj")
        with pytest.raises(ResolutionError, match="not a string"):
            mgr.resolve_ref(ref)


# ---------------------------------------------------------------------------
# SecretsManager — exec provider
# ---------------------------------------------------------------------------

class TestExecProvider:
    def _make_script(self, tmp_path: Path, content: str) -> Path:
        script = tmp_path / "helper.py"
        script.write_text(f"#!/usr/bin/env python3\n{content}")
        script.chmod(0o755)
        return script

    def test_simple_mode(self, tmp_path):
        script = self._make_script(tmp_path, dedent("""
            import sys
            # last arg is the ref_id
            print(sys.argv[-1] + "-resolved")
        """))
        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(script),
                    "jsonOnly": False,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="mykey")
        assert mgr.resolve_ref(ref) == "mykey-resolved"

    def test_json_protocol(self, tmp_path):
        script = self._make_script(tmp_path, dedent("""
            import sys, json
            payload = json.load(sys.stdin)
            ids = payload["ids"]
            print(json.dumps({"protocolVersion": 1, "values": {i: i + "-val" for i in ids}}))
        """))
        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(script),
                    "jsonOnly": True,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="token123")
        assert mgr.resolve_ref(ref) == "token123-val"

    def test_json_protocol_error_key(self, tmp_path):
        script = self._make_script(tmp_path, dedent("""
            import sys, json
            payload = json.load(sys.stdin)
            ids = payload["ids"]
            print(json.dumps({"protocolVersion": 1, "values": {}, "errors": {ids[0]: {"message": "not found"}}}))
        """))
        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(script),
                    "jsonOnly": True,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="missing")
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_ref(ref)

    def test_nonzero_exit(self, tmp_path):
        script = self._make_script(tmp_path, dedent("""
            import sys
            print("oops", file=sys.stderr)
            sys.exit(1)
        """))
        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(script),
                    "jsonOnly": False,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="x")
        with pytest.raises(ResolutionError, match="exited 1"):
            mgr.resolve_ref(ref)

    def test_command_not_found(self):
        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": "/nonexistent/binary",
                    "jsonOnly": False,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="x")
        with pytest.raises(ResolutionError, match="command not found"):
            mgr.resolve_ref(ref)

    def test_symlink_blocked_by_default(self, tmp_path):
        real = tmp_path / "real.py"
        real.write_text("#!/usr/bin/env python3\nprint('hi')")
        real.chmod(0o755)
        link = tmp_path / "link.py"
        link.symlink_to(real)

        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(link),
                    "jsonOnly": False,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="x")
        with pytest.raises(ResolutionError, match="symlink"):
            mgr.resolve_ref(ref)

    def test_symlink_allowed_when_configured(self, tmp_path, monkeypatch):
        real = tmp_path / "real.py"
        real.write_text("#!/usr/bin/env python3\nprint('resolved')")
        real.chmod(0o755)
        link = tmp_path / "link.py"
        link.symlink_to(real)

        mgr = SecretsManager({
            "providers": {
                "myexec": {
                    "source": "exec",
                    "command": str(link),
                    "jsonOnly": False,
                    "allowSymlinkCommand": True,
                }
            }
        })
        ref = SecretRef(source=SecretSource.EXEC, provider="myexec", id="x")
        assert mgr.resolve_ref(ref) == "resolved"


# ---------------------------------------------------------------------------
# SecretsManager — keychain provider
# ---------------------------------------------------------------------------

class TestKeychainProvider:
    def _mgr(self, backend: str = "auto") -> SecretsManager:
        return SecretsManager({
            "providers": {
                "keychain": {"source": "keychain", "service": "pyclaw-test", "backend": backend}
            }
        })

    def _ref(self, account: str) -> SecretRef:
        return SecretRef(source=SecretSource.KEYCHAIN, provider="keychain", id=account)

    # --- macOS security CLI ---

    def test_security_backend_success(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "secret-from-keychain\n"
        mock_result.stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)

        mgr = self._mgr("security")
        assert mgr.resolve_ref(self._ref("myaccount")) == "secret-from-keychain"

    def test_security_backend_not_found(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 44
        mock_result.stdout = ""
        mock_result.stderr = "The specified item could not be found in the keychain."
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)

        mgr = self._mgr("security")
        with pytest.raises(ResolutionError, match="Keychain lookup failed"):
            mgr.resolve_ref(self._ref("missing"))

    def test_security_binary_missing(self, monkeypatch):
        import subprocess as sp
        def raise_fnf(*a, **kw):
            raise FileNotFoundError("security not found")
        monkeypatch.setattr(sp, "run", raise_fnf)

        mgr = self._mgr("security")
        with pytest.raises(ResolutionError, match="security.*command not found"):
            mgr.resolve_ref(self._ref("x"))

    def test_security_timeout(self, monkeypatch):
        import subprocess as sp
        def raise_timeout(*a, **kw):
            raise sp.TimeoutExpired(cmd="security", timeout=5)
        monkeypatch.setattr(sp, "run", raise_timeout)

        mgr = self._mgr("security")
        with pytest.raises(ResolutionError, match="timed out"):
            mgr.resolve_ref(self._ref("x"))

    # --- keyring library ---

    def test_keyring_backend_success(self, monkeypatch):
        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = "keyring-secret"
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

        mgr = self._mgr("keyring")
        assert mgr.resolve_ref(self._ref("myaccount")) == "keyring-secret"
        fake_keyring.get_password.assert_called_once_with("pyclaw-test", "myaccount")

    def test_keyring_entry_missing(self, monkeypatch):
        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = None
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

        mgr = self._mgr("keyring")
        with pytest.raises(ResolutionError, match="not found"):
            mgr.resolve_ref(self._ref("missing"))

    def test_keyring_not_installed(self, monkeypatch):
        # Simulate keyring not importable
        monkeypatch.setitem(sys.modules, "keyring", None)

        mgr = self._mgr("keyring")
        with pytest.raises(ResolutionError, match="keyring.*library is required"):
            mgr.resolve_ref(self._ref("x"))

    # --- auto backend ---

    def test_auto_uses_security_on_macos(self, monkeypatch):
        import platform, subprocess as sp
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "auto-mac-secret"
        mock_result.stderr = ""
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock_result
        monkeypatch.setattr(sp, "run", fake_run)

        mgr = self._mgr("auto")
        assert mgr.resolve_ref(self._ref("acc")) == "auto-mac-secret"
        assert "security" in calls[0]

    def test_auto_uses_keyring_on_linux(self, monkeypatch):
        import platform
        monkeypatch.setattr(platform, "system", lambda: "Linux")

        fake_keyring = MagicMock()
        fake_keyring.get_password.return_value = "linux-secret"
        monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

        mgr = self._mgr("auto")
        assert mgr.resolve_ref(self._ref("acc")) == "linux-secret"

    def test_unknown_backend_raises(self):
        mgr = SecretsManager({
            "providers": {
                "kc": {"source": "keychain", "service": "s", "backend": "nosuchbackend"}
            }
        })
        ref = SecretRef(source=SecretSource.KEYCHAIN, provider="kc", id="x")
        with pytest.raises(ResolutionError, match="Unknown keychain backend"):
            mgr.resolve_ref(ref)

    # --- is_ref recognises keychain source ---

    def test_is_ref_keychain(self):
        assert SecretRef.is_ref({"source": "keychain", "id": "myaccount"})

    # --- resolve_raw with keychain ref ---

    def test_resolve_raw_keychain(self, monkeypatch):
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "raw-keychain-val"
        mock_result.stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **kw: mock_result)

        mgr = SecretsManager({
            "providers": {
                "keychain": {"source": "keychain", "service": "pyclaw", "backend": "security"}
            }
        })
        raw = {
            "channels": {
                "telegram": {
                    "botToken": {"source": "keychain", "provider": "keychain", "id": "telegram-bot"}
                }
            }
        }
        result = mgr.resolve_raw(raw)
        assert result["channels"]["telegram"]["botToken"] == "raw-keychain-val"


# ---------------------------------------------------------------------------
# SecretsManager — resolve_raw (walk + ${VAR} syntax)
# ---------------------------------------------------------------------------

class TestResolveRaw:
    def test_replaces_secret_ref_dict(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tg-12345")
        mgr = SecretsManager()
        raw = {
            "channels": {
                "telegram": {
                    "botToken": {"source": "env", "provider": "default", "id": "BOT_TOKEN"}
                }
            }
        }
        result = mgr.resolve_raw(raw)
        assert result["channels"]["telegram"]["botToken"] == "tg-12345"

    def test_resolves_legacy_env_syntax(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-abc")
        mgr = SecretsManager()
        raw = {"providers": {"anthropic": {"apiKey": "${API_KEY}"}}}
        result = mgr.resolve_raw(raw)
        assert result["providers"]["anthropic"]["apiKey"] == "sk-abc"

    def test_skips_secrets_subtree(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "resolved")
        mgr = SecretsManager()
        raw = {
            "secrets": {
                "providers": {
                    "default": {"source": "env"}
                }
            },
            "other": "${SOME_VAR}",
        }
        result = mgr.resolve_raw(raw)
        # secrets subtree is preserved as-is
        assert result["secrets"]["providers"]["default"]["source"] == "env"
        # other fields are resolved
        assert result["other"] == "resolved"

    def test_walks_list(self, monkeypatch):
        monkeypatch.setenv("ITEM_VAL", "hello")
        mgr = SecretsManager()
        raw = {"items": ["${ITEM_VAL}", "plain"]}
        result = mgr.resolve_raw(raw)
        assert result["items"] == ["hello", "plain"]

    def test_passthrough_non_string_scalars(self):
        mgr = SecretsManager()
        raw = {"num": 42, "flag": True, "nothing": None}
        result = mgr.resolve_raw(raw)
        assert result == {"num": 42, "flag": True, "nothing": None}

    def test_unset_env_var_returns_original(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
        mgr = SecretsManager()
        raw = {"key": "${MISSING_VAR_XYZ}"}
        result = mgr.resolve_raw(raw)
        # Falls back to original ${...} string
        assert result["key"] == "${MISSING_VAR_XYZ}"

    def test_resolution_error_returns_none(self, monkeypatch):
        monkeypatch.delenv("NO_SUCH_VAR", raising=False)
        mgr = SecretsManager()
        raw = {
            "key": {"source": "env", "provider": "default", "id": "NO_SUCH_VAR"}
        }
        result = mgr.resolve_raw(raw)
        assert result["key"] is None


# ---------------------------------------------------------------------------
# SecretsManager — reload
# ---------------------------------------------------------------------------

class TestReload:
    def test_reload_without_new_cfg(self, monkeypatch):
        monkeypatch.setenv("R_VAR", "v1")
        mgr = SecretsManager()
        ref = SecretRef(source=SecretSource.ENV, provider="default", id="R_VAR")
        mgr.resolve_ref(ref)
        monkeypatch.setenv("R_VAR", "v2")
        mgr.reload()
        assert mgr.resolve_ref(ref) == "v2"

    def test_reload_with_new_cfg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("INIT_VAR", "init")
        mgr = SecretsManager()

        # After reload with new cfg, unknown provider is now configured
        secret_file = tmp_path / "s.json"
        secret_file.write_text(json.dumps({"key": "file-value"}))
        secret_file.chmod(0o600)

        mgr.reload({
            "providers": {
                "myfile": {"source": "file", "path": str(secret_file), "mode": "json"}
            }
        })
        ref = SecretRef(source=SecretSource.FILE, provider="myfile", id="/key")
        assert mgr.resolve_ref(ref) == "file-value"


# ---------------------------------------------------------------------------
# Integration: ConfigLoader uses SecretsManager
# ---------------------------------------------------------------------------

class TestConfigLoaderIntegration:
    def test_loader_resolves_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TG_TOKEN", "tg-resolved")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
channels:
  telegram:
    enabled: true
    botToken:
      source: env
      provider: default
      id: TG_TOKEN
""")
        from pyclaw.config.loader import ConfigLoader
        loader = ConfigLoader(str(config_file))
        cfg = loader.load()
        assert cfg.channels.telegram.bot_token == "tg-resolved"

    def test_loader_resolves_legacy_syntax(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHRO_KEY", "sk-anthropic-123")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
providers:
  anthropic:
    enabled: true
    apiKey: "${ANTHRO_KEY}"
""")
        from pyclaw.config.loader import ConfigLoader
        loader = ConfigLoader(str(config_file))
        cfg = loader.load()
        assert cfg.providers.anthropic.api_key == "sk-anthropic-123"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Inline ${source:id} syntax
# ---------------------------------------------------------------------------

class TestInlineSecretSyntax:
    """Tests for the extended ${source:id} inline secret reference syntax."""

    def test_bare_env_var_still_works(self, monkeypatch):
        """${VAR} without colon still resolves as env (backward compat)."""
        monkeypatch.setenv("MY_KEY", "abc123")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        assert mgr.resolve_raw("${MY_KEY}") == "abc123"

    def test_explicit_env_prefix(self, monkeypatch):
        """${env:VAR} explicit env prefix."""
        monkeypatch.setenv("MY_SECRET", "hello")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        assert mgr.resolve_raw("${env:MY_SECRET}") == "hello"

    def test_env_prefix_unset_returns_original(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        result = mgr.resolve_raw("${env:DEFINITELY_NOT_SET}")
        assert result == "${env:DEFINITELY_NOT_SET}"

    def test_file_prefix_reads_file(self, tmp_path):
        """${file:/path/to/file} reads file content as singleValue."""
        secret_file = tmp_path / "api_key.txt"
        secret_file.write_text("  sk-supersecret  \n")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        result = mgr.resolve_raw(f"${{file:{secret_file}}}")
        assert result == "sk-supersecret"

    def test_file_prefix_missing_returns_original(self):
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        ref = "${file:/does/not/exist.txt}"
        result = mgr.resolve_raw(ref)
        assert result == ref

    def test_keychain_prefix_uses_configured_provider(self, monkeypatch):
        """${keychain:Account} routes through configured keychain provider."""
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({"providers": {"keychain": {"source": "keychain", "service": "myapp"}}})

        def fake_security(service, account):
            assert service == "myapp"
            assert account == "OpenAI Key"
            return "sk-real"

        monkeypatch.setattr(mgr, "_keychain_via_security", fake_security)
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        result = mgr.resolve_raw("${keychain:OpenAI Key}")
        assert result == "sk-real"

    def test_keychain_prefix_uses_default_provider_when_unconfigured(self, monkeypatch):
        """${keychain:Account} falls back to default service=pyclaw when no provider."""
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})  # no keychain provider configured

        def fake_security(service, account):
            assert service == "pyclaw"   # default
            assert account == "MyToken"
            return "tok-xyz"

        monkeypatch.setattr(mgr, "_keychain_via_security", fake_security)
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        result = mgr.resolve_raw("${keychain:MyToken}")
        assert result == "tok-xyz"

    def test_keychain_prefix_failure_returns_original(self, monkeypatch):
        """On keychain error the original ${...} string is returned."""
        from pyclaw.secrets.manager import SecretsManager
        from pyclaw.secrets.manager import ResolutionError
        mgr = SecretsManager({})

        monkeypatch.setattr(mgr, "_keychain_via_security", lambda s, a: (_ for _ in ()).throw(ResolutionError("not found")))
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        result = mgr.resolve_raw("${keychain:BadKey}")
        assert result == "${keychain:BadKey}"

    def test_named_provider_via_inline_syntax(self, monkeypatch):
        """${provider_name:id} routes through a named configured provider."""
        monkeypatch.setenv("MY_CUSTOM_VAR", "from-named-provider")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({
            "providers": {
                "myenv": {"source": "env"},
            }
        })
        result = mgr.resolve_raw("${myenv:MY_CUSTOM_VAR}")
        assert result == "from-named-provider"

    def test_unknown_source_returns_original(self):
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        ref = "${vault:some/path}"
        result = mgr.resolve_raw(ref)
        assert result == ref

    def test_inline_syntax_in_nested_config(self, monkeypatch):
        """${...} inside a nested dict is resolved during resolve_raw walk."""
        monkeypatch.setenv("EMBED_KEY", "sk-embed-123")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        data = {
            "memory": {
                "embedding": {
                    "enabled": True,
                    "apiKey": "${env:EMBED_KEY}",
                }
            }
        }
        resolved = mgr.resolve_raw(data)
        assert resolved["memory"]["embedding"]["apiKey"] == "sk-embed-123"

    def test_inline_syntax_in_list(self, monkeypatch):
        monkeypatch.setenv("K1", "val1")
        monkeypatch.setenv("K2", "val2")
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        result = mgr.resolve_raw(["${env:K1}", "${env:K2}", "plain"])
        assert result == ["val1", "val2", "plain"]

    def test_non_secret_strings_untouched(self, monkeypatch):
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        assert mgr.resolve_raw("just a string") == "just a string"
        assert mgr.resolve_raw("http://example.com") == "http://example.com"

    def test_file_prefix_with_tilde_expansion(self, tmp_path, monkeypatch):
        """${file:~/path} expands the tilde."""
        secret_file = tmp_path / ".secret"
        secret_file.write_text("tilde-value")
        monkeypatch.setenv("HOME", str(tmp_path))
        from pyclaw.secrets.manager import SecretsManager
        mgr = SecretsManager({})
        result = mgr.resolve_raw("${file:~/.secret}")
        assert result == "tilde-value"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dedent(text: str) -> str:
    """Strip common leading whitespace (poor man's textwrap.dedent)."""
    import textwrap
    return textwrap.dedent(text).lstrip("\n")
