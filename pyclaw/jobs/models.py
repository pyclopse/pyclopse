"""Job dataclasses for the scheduler."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pathlib import Path


class JobStatus(str, Enum):
    """Job status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobTrigger(str, Enum):
    """Job trigger type."""
    CRON = "cron"
    INTERVAL = "interval"
    MANUAL = "manual"
    ONESHOT = "oneshot"


@dataclass
class Job:
    """Job definition."""
    id: str
    name: str
    command: str
    trigger: JobTrigger
    enabled: bool = True
    
    # Cron/interval specific
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    
    # Scheduling
    next_run: Optional[datetime] = None
    last_run: Optional[datetime] = None
    last_result: Optional[Dict[str, Any]] = None
    
    # Status
    status: JobStatus = JobStatus.PENDING
    run_count: int = 0
    failure_count: int = 0
    
    # Constraints
    timeout: Optional[int] = 300  # 5 minutes default
    retry_count: int = 0
    max_retries: int = 0
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetimes to ISO format
        for key in ["next_run", "last_run", "created_at", "updated_at"]:
            if data.get(key):
                data[key] = data[key].isoformat() + "Z"
            else:
                data[key] = None
        # Convert enums to strings
        data["trigger"] = self.trigger.value
        data["status"] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        """Create Job from dictionary."""
        # Convert ISO strings back to datetime
        for key in ["next_run", "last_run", "created_at", "updated_at"]:
            if data.get(key):
                # Handle both with and without Z suffix
                iso_str = data[key].rstrip("Z")
                data[key] = datetime.fromisoformat(iso_str)
            else:
                data[key] = None
        
        # Convert string enums
        data["trigger"] = JobTrigger(data["trigger"])
        data["status"] = JobStatus(data["status"])
        
        return cls(**data)
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())
    
    @classmethod
    def from_json(cls, json_str: str) -> "Job":
        """Create Job from JSON string."""
        return cls.from_dict(json.loads(json_str))
    
    def update_status(self, status: JobStatus, result: Optional[Dict[str, Any]] = None) -> None:
        """Update job status."""
        self.status = status
        self.updated_at = datetime.utcnow()
        if result:
            self.last_result = result
        if status == JobStatus.RUNNING:
            self.last_run = datetime.utcnow()
            self.run_count += 1
        if status == JobStatus.FAILED:
            self.failure_count += 1
    
    def should_retry(self) -> bool:
        """Check if job should be retried."""
        return (
            self.max_retries > 0 and
            self.failure_count < self.max_retries and
            self.status == JobStatus.FAILED
        )


@dataclass
class JobRun:
    """Record of a single job execution."""
    id: str
    job_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: JobStatus = JobStatus.PENDING
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat() + "Z"
        if self.ended_at:
            data["ended_at"] = self.ended_at.isoformat() + "Z"
        data["status"] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRun":
        """Create JobRun from dictionary."""
        data["started_at"] = datetime.fromisoformat(data["started_at"].rstrip("Z"))
        if data.get("ended_at"):
            data["ended_at"] = datetime.fromisoformat(data["ended_at"].rstrip("Z"))
        data["status"] = JobStatus(data["status"])
        return cls(**data)
