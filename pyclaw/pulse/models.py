"""Pulse system models."""
from dataclasses import dataclass, field
from datetime import datetime
from pyclaw.utils.time import now
from typing import Optional, Dict, Any, List
from enum import Enum


class PulseStatus(str, Enum):
    """Status of a pulse task execution."""
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class PulseActiveHours:
    """Defines active hours for pulse execution."""
    
    def __init__(self, start: str = "00:00", end: str = "23:59"):
        """
        Args:
            start: Start time in HH:MM format (24-hour)
            end: End time in HH:MM format (24-hour)
        """
        self.start = start
        self.end = end
    
    def is_active(self, dt: Optional[datetime] = None) -> bool:
        """Check if current time is within active hours."""
        if dt is None:
            dt = now()
        
        current_time = dt.strftime("%H:%M")
        
        # Handle overnight ranges (e.g., 22:00 to 06:00)
        if self.start > self.end:
            return current_time >= self.start or current_time <= self.end
        
        return self.start <= current_time <= self.end


@dataclass
class PulseTask:
    """Represents a pulse task configuration."""
    agent_id: str
    interval_seconds: int
    prompt: str
    active_hours: Optional[PulseActiveHours] = None
    enabled: bool = True
    last_run: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_config(cls, agent_id: str, config: Dict[str, Any]) -> "PulseTask":
        """Create PulseTask from configuration dict."""
        interval_str = config.get("every", "30m")
        interval_seconds = cls._parse_interval(interval_str)
        
        active_hours = None
        if "activeHours" in config:
            ah = config["activeHours"]
            active_hours = PulseActiveHours(
                start=ah.get("start", "00:00"),
                end=ah.get("end", "23:59")
            )
        
        return cls(
            agent_id=agent_id,
            interval_seconds=interval_seconds,
            prompt=config.get("prompt", "Check for updates."),
            active_hours=active_hours,
            enabled=config.get("enabled", True),
        )
    
    @staticmethod
    def _parse_interval(interval_str: str) -> int:
        """Parse interval string like '30m', '1h', '5s' to seconds."""
        interval_str = interval_str.lower().strip()
        
        if interval_str.endswith("s"):
            return int(interval_str[:-1])
        elif interval_str.endswith("m"):
            return int(interval_str[:-1]) * 60
        elif interval_str.endswith("h"):
            return int(interval_str[:-1]) * 3600
        elif interval_str.endswith("d"):
            return int(interval_str[:-1]) * 86400
        else:
            # Default to seconds if no unit
            return int(interval_str)


@dataclass
class PulseResult:
    """Result of a pulse task execution."""
    task: PulseTask
    status: PulseStatus
    message: Optional[str] = None
    executed_at: datetime = field(default_factory=datetime.now)
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_success(self) -> bool:
        return self.status == PulseStatus.SUCCESS
