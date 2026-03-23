"""Audit logging for security events."""

import json
import logging
import os
from datetime import datetime, timedelta
from pyclaw.utils.time import now
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class AuditEvent:
    """Single audit log entry serialised as a JSON line.

    All fields are optional except ``timestamp`` and ``event_type``.
    Instances are created by :class:`AuditLogger` and serialised via
    :func:`dataclasses.asdict` before being written to the log file.

    Attributes:
        timestamp (str): ISO-8601 timestamp of when the event occurred.
        event_type (str): Category of the event (e.g. ``"command_execution"``,
            ``"session_start"``, ``"authentication"``).
        agent_id (Optional[str]): Identifier of the agent involved.
        session_id (Optional[str]): Identifier of the conversation session.
        user_id (Optional[str]): External user identifier
            (Telegram/Slack user ID).
        channel (Optional[str]): Channel name or type (e.g. ``"telegram"``).
        command (Optional[str]): The command string for execution events.
        action (Optional[str]): Secondary action descriptor
            (e.g. ``"approved"``, ``"denied"``, tool name).
        status (str): Outcome of the event. Defaults to ``"success"``.
        details (Dict[str, Any]): Freeform key-value pairs with additional
            context.
        ip_address (Optional[str]): Remote IP address when applicable.
    """

    timestamp: str
    event_type: str
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    channel: Optional[str] = None
    command: Optional[str] = None
    action: Optional[str] = None
    status: str = "success"
    details: Dict[str, Any] = field(default_factory=dict)
    ip_address: Optional[str] = None


class AuditLogger:
    """Security audit logger that writes JSON-lines to a rotating log file.

    Each log entry is a single JSON object on its own line produced by
    serialising an :class:`AuditEvent` dataclass.  Old entries are pruned
    via :meth:`rotate_logs` based on :attr:`retention_days`.

    Attributes:
        log_file (Path): Resolved absolute path to the audit log file.
        retention_days (int): Number of days to retain log entries before
            rotation.
        _logger (logging.Logger): Underlying Python logger configured with
            a :class:`logging.FileHandler` that writes to :attr:`log_file`.
    """

    def __init__(
        self,
        log_file: str = "~/.pyclaw/logs/audit.log",
        retention_days: int = 90,
    ) -> None:
        """Initialise the audit logger.

        Creates the log directory if it does not exist and attaches a
        :class:`logging.FileHandler` to the ``"pyclaw.audit"`` logger.

        Args:
            log_file (str): Path to the audit log file.  Tilde expansion is
                applied. Defaults to ``"~/.pyclaw/logs/audit.log"``.
            retention_days (int): Days to keep log entries before rotating
                them out. Defaults to 90.
        """
        self.log_file = Path(os.path.expanduser(log_file))
        self.retention_days = retention_days
        self._logger = logging.getLogger("pyclaw.audit")
        self._setup_logger()

    def _setup_logger(self) -> None:
        """Configure the underlying Python logger with a file handler.

        Creates the parent directory for :attr:`log_file`, attaches a
        :class:`logging.FileHandler` that emits the raw message (no
        timestamp prefix — the JSON payload already contains one), and
        disables propagation to avoid duplicate log lines.
        """
        # Ensure directory exists
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # Create file handler
        handler = logging.FileHandler(self.log_file)
        handler.setLevel(logging.INFO)

        # Set format
        formatter = logging.Formatter(
            "%(message)s",  # JSON is logged as message
        )
        handler.setFormatter(formatter)

        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    async def log(
        self,
        event_type: str,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
        command: Optional[str] = None,
        action: Optional[str] = None,
        status: str = "success",
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """Log a security event as a JSON line to the audit log file.

        Constructs an :class:`AuditEvent` and writes it as a single JSON
        object using the underlying Python logger.

        Args:
            event_type (str): Category of the event.
            agent_id (Optional[str]): Agent identifier. Defaults to None.
            session_id (Optional[str]): Session identifier. Defaults to None.
            user_id (Optional[str]): User identifier. Defaults to None.
            channel (Optional[str]): Channel name. Defaults to None.
            command (Optional[str]): Command string for execution events.
                Defaults to None.
            action (Optional[str]): Secondary action label. Defaults to None.
            status (str): Event outcome string. Defaults to ``"success"``.
            details (Optional[Dict[str, Any]]): Extra key-value context.
                Defaults to an empty dict.
            ip_address (Optional[str]): Remote IP address. Defaults to None.
        """
        event = AuditEvent(
            timestamp=now().isoformat(),
            event_type=event_type,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            command=command,
            action=action,
            status=status,
            details=details or {},
            ip_address=ip_address,
        )

        # Log as JSON
        self._logger.info(json.dumps(asdict(event)))

    async def log_command_execution(
        self,
        command: str,
        approved: bool,
        agent_id: str,
        session_id: str,
        user_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Log a command execution approval or denial event.

        Args:
            command (str): The command string that was evaluated.
            approved (bool): True if the command was approved, False if denied.
            agent_id (str): Identifier of the agent that requested execution.
            session_id (str): Identifier of the current session.
            user_id (Optional[str]): User identifier. Defaults to None.
            reason (Optional[str]): Human-readable reason for the decision.
                Defaults to None.
        """
        await self.log(
            event_type="command_execution",
            command=command,
            action="approved" if approved else "denied",
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            status="success" if approved else "denied",
            details={"reason": reason} if reason else {},
        )

    async def log_session_start(
        self,
        session_id: str,
        agent_id: str,
        channel: str,
        user_id: Optional[str] = None,
    ) -> None:
        """Log a session start event.

        Args:
            session_id (str): Identifier of the session that started.
            agent_id (str): Identifier of the agent handling the session.
            channel (str): Channel name (e.g. ``"telegram"``, ``"slack"``).
            user_id (Optional[str]): User identifier. Defaults to None.
        """
        await self.log(
            event_type="session_start",
            agent_id=agent_id,
            session_id=session_id,
            channel=channel,
            user_id=user_id,
        )

    async def log_session_end(
        self,
        session_id: str,
        agent_id: str,
        message_count: int = 0,
    ) -> None:
        """Log a session end event.

        Args:
            session_id (str): Identifier of the session that ended.
            agent_id (str): Identifier of the agent that handled the session.
            message_count (int): Total number of messages exchanged during
                the session. Defaults to 0.
        """
        await self.log(
            event_type="session_end",
            agent_id=agent_id,
            session_id=session_id,
            details={"message_count": message_count},
        )

    async def log_message_received(
        self,
        session_id: str,
        agent_id: str,
        channel: str,
        user_id: Optional[str] = None,
        message_preview: Optional[str] = None,
    ) -> None:
        """Log an incoming message event with a truncated preview.

        Args:
            session_id (str): Identifier of the session receiving the message.
            agent_id (str): Identifier of the agent handling the session.
            channel (str): Channel on which the message arrived.
            user_id (Optional[str]): User identifier. Defaults to None.
            message_preview (Optional[str]): First 100 characters of the
                message text for audit purposes. Defaults to None.
        """
        await self.log(
            event_type="message_received",
            agent_id=agent_id,
            session_id=session_id,
            channel=channel,
            user_id=user_id,
            details={"preview": message_preview[:100] if message_preview else None},
        )

    async def log_tool_execution(
        self,
        tool_name: str,
        agent_id: str,
        session_id: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Log an MCP tool execution event.

        Args:
            tool_name (str): Name of the MCP tool that was executed.
            agent_id (str): Identifier of the agent that invoked the tool.
            session_id (str): Identifier of the session during which the tool
                was called.
            success (bool): True if the tool returned successfully.
            error (Optional[str]): Error message if the tool failed.
                Defaults to None.
        """
        await self.log(
            event_type="tool_execution",
            action=tool_name,
            agent_id=agent_id,
            session_id=session_id,
            status="success" if success else "error",
            details={"error": error} if error else {},
        )

    async def log_authentication(
        self,
        user_id: str,
        channel: str,
        success: bool,
        reason: Optional[str] = None,
    ) -> None:
        """Log an authentication attempt event.

        Args:
            user_id (str): Identifier of the user attempting to authenticate.
            channel (str): Channel on which the authentication occurred.
            success (bool): True if authentication succeeded.
            reason (Optional[str]): Human-readable reason for failure, or
                None on success. Defaults to None.
        """
        await self.log(
            event_type="authentication",
            channel=channel,
            user_id=user_id,
            status="success" if success else "failure",
            details={"reason": reason} if reason else {},
        )

    async def log_config_change(
        self,
        agent_id: str,
        field: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        """Log a configuration change event.

        Args:
            agent_id (str): Identifier of the agent whose config changed.
            field (str): Dot-notation path of the config field that changed.
            old_value (Any): Previous value (stringified for the log).
            new_value (Any): New value (stringified for the log).
        """
        await self.log(
            event_type="config_change",
            agent_id=agent_id,
            action=field,
            details={
                "old_value": str(old_value),
                "new_value": str(new_value),
            },
        )

    def rotate_logs(self) -> None:
        """Rotate audit logs based on the configured retention policy.

        Reads the log file line-by-line, separating entries older than
        :attr:`retention_days` from those within the retention window.
        Recent entries are written back to :attr:`log_file`; old entries are
        appended to a date-stamped archive file in the same directory.
        Malformed lines (not valid JSON or missing ``timestamp``) are kept
        in the active log file.

        Does nothing if the log file does not exist.
        """
        if not self.log_file.exists():
            return

        cutoff_date = now() - timedelta(days=self.retention_days)

        # Read existing logs
        rotated_entries = []
        remaining_entries = []

        try:
            with open(self.log_file, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        entry_date = datetime.fromisoformat(
                            entry["timestamp"].replace("Z", "+00:00")
                        )
                        if entry_date.replace(tzinfo=None) < cutoff_date:
                            rotated_entries.append(line)
                        else:
                            remaining_entries.append(line)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        # Keep malformed entries
                        remaining_entries.append(line)

            # Write remaining entries back
            with open(self.log_file, "w") as f:
                f.writelines(remaining_entries)

            # Optionally archive rotated entries
            if rotated_entries:
                archive_file = self.log_file.with_suffix(
                    f".{now().strftime('%Y%m%d')}.log"
                )
                with open(archive_file, "a") as f:
                    f.writelines(rotated_entries)

        except Exception as e:
            self._logger.error(f"Error rotating logs: {e}")

    async def run_security_audit(self) -> Dict[str, Any]:
        """Run a basic security audit check against the audit log.

        Checks whether the log file exists and whether its size exceeds
        100 MB.  Also checks whether the oldest entry pre-dates the
        retention window and adds an informational finding if so.

        Returns:
            Dict[str, Any]: Audit results with two keys:

            - ``"findings"`` — list of finding dicts, each with
              ``"severity"`` (``"error"``, ``"warning"``, or ``"info"``),
              ``"check"`` (identifier), and ``"message"`` (description).
            - ``"summary"`` — dict with ``"total_findings"``, ``"warnings"``,
              and ``"errors"`` counts.
        """
        findings = []

        # Check if log file exists and is writable
        if not self.log_file.exists():
            findings.append({
                "severity": "warning",
                "check": "audit_log_exists",
                "message": "Audit log file does not exist",
            })
        else:
            # Check file size
            file_size = self.log_file.stat().st_size
            if file_size > 100 * 1024 * 1024:  # 100MB
                findings.append({
                    "severity": "warning",
                    "check": "audit_log_size",
                    "message": f"Audit log file is large: {file_size / 1024 / 1024:.1f}MB",
                })

        # Check retention
        if self.log_file.exists():
            try:
                with open(self.log_file, "r") as f:
                    first_line = f.readline()
                    if first_line:
                        entry = json.loads(first_line)
                        first_date = datetime.fromisoformat(
                            entry["timestamp"].replace("Z", "+00:00")
                        )
                        age_days = (now() - first_date.replace(tzinfo=None)).days
                        if age_days > self.retention_days:
                            findings.append({
                                "severity": "info",
                                "check": "audit_retention",
                                "message": f"Logs older than retention period exist",
                            })
            except Exception:
                pass

        return {
            "findings": findings,
            "summary": {
                "total_findings": len(findings),
                "warnings": len([f for f in findings if f["severity"] == "warning"]),
                "errors": len([f for f in findings if f["severity"] == "error"]),
            },
        }
