"""Job models using Pydantic v2 discriminated unions."""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Run types — what the job executes
# ---------------------------------------------------------------------------

class CommandRun(BaseModel):
    """Execute a shell command."""
    kind: Literal["command"] = "command"
    command: str


class AgentRun(BaseModel):
    """Send a message to a named agent and deliver its response."""
    kind: Literal["agent"] = "agent"
    agent: str
    message: str
    model: Optional[str] = None   # overrides agent's default model if set


JobRunType = Annotated[Union[CommandRun, AgentRun], Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Schedule types — when the job runs
# ---------------------------------------------------------------------------

class CronSchedule(BaseModel):
    """Standard 5-field cron expression."""
    kind: Literal["cron"] = "cron"
    expr: str
    timezone: str = "UTC"
    stagger_seconds: int = 0      # random jitter up to N seconds


class IntervalSchedule(BaseModel):
    """Fixed interval between runs."""
    kind: Literal["interval"] = "interval"
    seconds: int


class AtSchedule(BaseModel):
    """One-shot: run once at an absolute datetime then delete/disable."""
    kind: Literal["at"] = "at"
    at: datetime


JobScheduleType = Annotated[
    Union[CronSchedule, IntervalSchedule, AtSchedule],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Delivery types — where results go
# ---------------------------------------------------------------------------

class DeliverNone(BaseModel):
    """Suppress output — run silently."""
    mode: Literal["none"] = "none"


class DeliverAnnounce(BaseModel):
    """Send result to a messaging channel."""
    mode: Literal["announce"] = "announce"
    channel: Optional[str] = None    # "telegram", "slack", etc.
    chat_id: Optional[str] = None    # channel-specific recipient ID


class DeliverWebhook(BaseModel):
    """HTTP POST the result to a URL."""
    mode: Literal["webhook"] = "webhook"
    url: str


DeliveryType = Annotated[
    Union[DeliverNone, DeliverAnnounce, DeliverWebhook],
    Field(discriminator="mode"),
]


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------

class FailureAlert(BaseModel):
    """Alert config when a job fails repeatedly."""
    alert_after: int = 3            # consecutive errors before alerting
    channel: Optional[str] = None
    chat_id: Optional[str] = None


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Job — the main model
# ---------------------------------------------------------------------------

class Job(BaseModel):
    """A scheduled job definition plus runtime state."""

    # Identity
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    tags: List[str] = Field(default_factory=list)

    # What to run
    run: JobRunType

    # When to run
    schedule: JobScheduleType

    # Where to deliver results
    deliver: DeliveryType = Field(default_factory=DeliverAnnounce)

    # Failure alerting
    on_failure: Optional[FailureAlert] = None

    # Execution constraints
    timeout_seconds: int = 300
    max_retries: int = 0
    delete_after_run: bool = False   # one-shot jobs delete themselves on success

    # Runtime state (managed by scheduler, not user-editable)
    status: JobStatus = JobStatus.PENDING
    next_run: Optional[datetime] = None
    last_run: Optional[datetime] = None
    last_result: Optional[Dict[str, Any]] = None
    run_count: int = 0
    failure_count: int = 0
    consecutive_errors: int = 0

    # Ownership — set at creation time by the agent/caller, never changed
    owner: Optional[str] = None

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# JobRun — record of a single execution
# ---------------------------------------------------------------------------

class JobRun(BaseModel):
    """Record of one job execution, written to per-job JSONL log."""
    id: str
    job_id: str
    job_name: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: JobStatus = JobStatus.RUNNING
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None

    def duration_ms(self) -> Optional[float]:
        if self.ended_at and self.started_at:
            return (self.ended_at - self.started_at).total_seconds() * 1000
        return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_jobs(jobs: Dict[str, Job], path: Path) -> None:
    """Atomically write jobs to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 2,
        "jobs": [job.model_dump(mode="json") for job in jobs.values()],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def load_jobs(path: Path) -> Dict[str, Job]:
    """Load jobs from JSON file, migrating v1 format if needed."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        version = data.get("version", 1)
        jobs = {}
        for raw in data.get("jobs", []):
            try:
                if version < 2:
                    raw = _migrate_v1(raw)
                job = Job.model_validate(raw)
                jobs[job.id] = job
            except Exception as e:
                import logging
                logging.getLogger("pyclaw.jobs").warning(f"Skipping invalid job: {e}")
        return jobs
    except Exception as e:
        import logging
        logging.getLogger("pyclaw.jobs").error(f"Error loading jobs: {e}")
        return {}


def _migrate_v1(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate a v1 flat job record to v2 nested format."""
    out = dict(raw)

    # Build run object
    if "run" not in out:
        command = out.pop("command", None)
        out["run"] = {"kind": "command", "command": command or ""}

    # Build schedule object
    if "schedule" not in out:
        trigger = out.pop("trigger", "interval")
        cron_expr = out.pop("cron_expression", None)
        interval_s = out.pop("interval_seconds", None)
        if trigger == "cron" and cron_expr:
            out["schedule"] = {"kind": "cron", "expr": cron_expr}
        elif interval_s:
            out["schedule"] = {"kind": "interval", "seconds": int(interval_s)}
        else:
            out["schedule"] = {"kind": "interval", "seconds": 3600}

    # Build deliver object
    if "deliver" not in out:
        ch = out.pop("target_channel", None)
        cid = out.pop("target_chat_id", None)
        out["deliver"] = {"mode": "announce", "channel": ch, "chat_id": cid}

    # Clean up flat fields that no longer exist
    for obsolete in ["target_channel", "target_chat_id", "trigger",
                      "cron_expression", "interval_seconds", "retry_count",
                      "last_result", "metadata"]:
        out.pop(obsolete, None)

    # Rename timeout → timeout_seconds
    if "timeout" in out and "timeout_seconds" not in out:
        out["timeout_seconds"] = out.pop("timeout")

    return out


def append_run_log(run: JobRun, runs_dir: Path) -> None:
    """Append a completed run to per-job JSONL log."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{run.job_id}.jsonl"
    with open(log_path, "a") as f:
        f.write(run.model_dump_json() + "\n")


def read_run_log(job_id: str, runs_dir: Path, limit: int = 20) -> List[JobRun]:
    """Read the last N runs for a job from JSONL log."""
    log_path = runs_dir / f"{job_id}.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()
    runs = []
    for line in lines[-limit:]:
        line = line.strip()
        if line:
            try:
                runs.append(JobRun.model_validate_json(line))
            except Exception:
                pass
    return runs
