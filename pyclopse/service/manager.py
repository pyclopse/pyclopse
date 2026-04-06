"""Cross-platform service manager — launchd (macOS) and systemd (Linux).

Handles install/uninstall/start/stop/restart/status/logs for running
pyclopse as a background service.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "com.pyclopse.gateway"
SYSTEMD_UNIT = "pyclopse.service"


def _find_pyclopse_bin() -> str:
    """Resolve the absolute path to the pyclopse binary.

    Checks (in order):
      1. uv tool bin: ~/.local/bin/pyclopse
      2. shutil.which("pyclopse")
      3. Fallback: `uv run python -m pyclopse` via the dev venv
    """
    # uv tool install location
    uv_bin = Path.home() / ".local" / "bin" / "pyclopse"
    if uv_bin.exists():
        return str(uv_bin)

    # Anywhere on PATH
    found = shutil.which("pyclopse")
    if found:
        return found

    # Dev mode: use the venv python with -m pyclopse
    return None


def _log_dir() -> Path:
    d = Path.home() / ".pyclopse" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# macOS launchd
# ---------------------------------------------------------------------------

class _Launchd:
    """macOS launchd service manager."""

    def __init__(self):
        self._plist_dir = Path.home() / "Library" / "LaunchAgents"
        self._plist_path = self._plist_dir / f"{SERVICE_NAME}.plist"

    def _build_plist(self) -> str:
        pyclopse_bin = _find_pyclopse_bin()
        log_path = _log_dir() / "service.log"

        if pyclopse_bin:
            program_args = f"""\
    <key>ProgramArguments</key>
    <array>
      <string>{pyclopse_bin}</string>
      <string>--headless</string>
    </array>"""
        else:
            # Dev mode fallback: use uv run
            uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
            program_args = f"""\
    <key>ProgramArguments</key>
    <array>
      <string>{uv_bin}</string>
      <string>run</string>
      <string>python</string>
      <string>-m</string>
      <string>pyclopse</string>
      <string>--headless</string>
    </array>"""

        return textwrap.dedent(f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_NAME}</string>
{program_args}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
""")

    def install(self) -> str:
        self._plist_dir.mkdir(parents=True, exist_ok=True)
        content = self._build_plist()
        self._plist_path.write_text(content)

        # Load the service
        subprocess.run(
            ["launchctl", "load", str(self._plist_path)],
            check=False,
        )
        return f"Installed: {self._plist_path}\nService will start on login."

    def uninstall(self) -> str:
        if self._plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(self._plist_path)],
                check=False,
            )
            self._plist_path.unlink()
            return f"Uninstalled: {self._plist_path}"
        return "Service not installed."

    def start(self) -> str:
        if not self._plist_path.exists():
            return "Service not installed. Run 'pyclopse service install' first."
        subprocess.run(
            ["launchctl", "start", SERVICE_NAME],
            check=False,
        )
        return "Service started."

    def stop(self) -> str:
        subprocess.run(
            ["launchctl", "stop", SERVICE_NAME],
            check=False,
        )
        return "Service stopped."

    def restart(self) -> str:
        self.stop()
        self.start()
        return "Service restarted."

    def status(self) -> str:
        result = subprocess.run(
            ["launchctl", "list", SERVICE_NAME],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return "Service not loaded."
        # Parse PID from output
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == SERVICE_NAME:
                pid = parts[0]
                status = parts[1]
                if pid == "-":
                    return f"Service loaded but not running (last exit: {status})"
                return f"Service running (PID: {pid})"
        return f"Service loaded.\n{result.stdout}"

    def logs(self, lines: int = 50) -> str:
        log_path = _log_dir() / "service.log"
        if not log_path.exists():
            return "No service log found."
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-lines:])


# ---------------------------------------------------------------------------
# Linux systemd
# ---------------------------------------------------------------------------

class _Systemd:
    """Linux systemd user service manager."""

    def __init__(self):
        self._unit_dir = Path.home() / ".config" / "systemd" / "user"
        self._unit_path = self._unit_dir / SYSTEMD_UNIT

    def _build_unit(self) -> str:
        pyclopse_bin = _find_pyclopse_bin()
        log_path = _log_dir() / "service.log"

        if pyclopse_bin:
            exec_start = f"{pyclopse_bin} --headless"
        else:
            uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
            exec_start = f"{uv_bin} run python -m pyclopse --headless"

        return textwrap.dedent(f"""\
[Unit]
Description=Pyclopse AI Agent Gateway
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=default.target
""")

    def install(self) -> str:
        self._unit_dir.mkdir(parents=True, exist_ok=True)
        self._unit_path.write_text(self._build_unit())
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT], check=False)
        return f"Installed: {self._unit_path}\nService enabled for login."

    def uninstall(self) -> str:
        if self._unit_path.exists():
            subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT], check=False)
            subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT], check=False)
            self._unit_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            return f"Uninstalled: {self._unit_path}"
        return "Service not installed."

    def start(self) -> str:
        if not self._unit_path.exists():
            return "Service not installed. Run 'pyclopse service install' first."
        subprocess.run(["systemctl", "--user", "start", SYSTEMD_UNIT], check=False)
        return "Service started."

    def stop(self) -> str:
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT], check=False)
        return "Service stopped."

    def restart(self) -> str:
        subprocess.run(["systemctl", "--user", "restart", SYSTEMD_UNIT], check=False)
        return "Service restarted."

    def status(self) -> str:
        result = subprocess.run(
            ["systemctl", "--user", "status", SYSTEMD_UNIT],
            capture_output=True, text=True,
        )
        return result.stdout or result.stderr or "Unknown status."

    def logs(self, lines: int = 50) -> str:
        # Try journalctl first, fall back to log file
        result = subprocess.run(
            ["journalctl", "--user-unit", SYSTEMD_UNIT, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        log_path = _log_dir() / "service.log"
        if log_path.exists():
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(all_lines[-lines:])
        return "No logs found."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_manager():
    """Return the platform-appropriate service manager."""
    if sys.platform == "darwin":
        return _Launchd()
    elif sys.platform.startswith("linux"):
        return _Systemd()
    else:
        raise RuntimeError(f"Service management not supported on {sys.platform}")
