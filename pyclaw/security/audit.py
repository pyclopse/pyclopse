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
    """Single audit log entry."""
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
    """Security audit logger that writes to file."""
    
    def __init__(
        self,
        log_file: str = "~/.pyclaw/logs/audit.log",
        retention_days: int = 90,
    ):
        self.log_file = Path(os.path.expanduser(log_file))
        self.retention_days = retention_days
        self._logger = logging.getLogger("pyclaw.audit")
        self._setup_logger()
    
    def _setup_logger(self) -> None:
        """Set up the logger with file handler."""
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
        """Log a security event."""
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
        """Log a command execution event."""
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
        """Log session start."""
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
        """Log session end."""
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
        """Log incoming message."""
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
        """Log tool execution."""
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
        """Log authentication attempt."""
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
        """Log configuration change."""
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
        """Rotate audit logs based on retention policy."""
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
        """Run a security audit check."""
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
