"""Job models using Pydantic v2 discriminated unions."""

import json
from datetime import datetime
from pyclaw.utils.time import now as _now
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Run types — what the job executes
# ---------------------------------------------------------------------------

class CommandRun(BaseModel):
    """Execute a shell command."""
    kind: Literal["command"] = "command"
    command: str


# ---------------------------------------------------------------------------
# Prompt presets — define the default include_* values for each preset
# ---------------------------------------------------------------------------

PRESET_DEFAULTS: Dict[str, Dict[str, bool]] = {
    "full": dict(
        personality=True, identity=True, rules=True, memory=True,
        user=True, agents=True, tools=True, skills=True,
    ),
    "minimal": dict(
        personality=True, identity=True, rules=True, memory=False,
        user=False, agents=False, tools=False, skills=True,
    ),
    "task": dict(
        personality=False, identity=False, rules=False, memory=False,
        user=False, agents=False, tools=False, skills=False,
    ),
}


class AgentRun(BaseModel):
    """Send a message to a named agent and deliver its response."""
    kind: Literal["agent"] = "agent"
    agent: str
    message: str
    model: Optional[str] = None   # overrides agent's default model if set

    # Session continuity
    session_mode: Literal["isolated", "persistent"] = "isolated"

    # Prompt preset — sets default values for all include_* flags below
    prompt_preset: Literal["full", "minimal", "task"] = "full"

    # Prompt composition — None means "use preset default", resolved at validation time
    include_personality: Optional[bool] = None
    include_identity:    Optional[bool] = None
    include_rules:       Optional[bool] = None
    include_memory:      Optional[bool] = None
    include_user:        Optional[bool] = None
    include_agents:      Optional[bool] = None
    include_tools:       Optional[bool] = None
    include_skills:      Optional[bool] = None

    # Optional skill filter — if set, only these named skills are injected (requires include_skills=True)
    # If None (default), all discovered skills are injected when include_skills=True.
    skills: Optional[List[str]] = None

    # Optional list of file paths whose contents are injected into the system prompt,
    # after the instruction. Paths are expanded (~ supported). Missing files are skipped.
    include_files: Optional[List[str]] = None

    # Optional instruction appended to the system prompt (after all include_* content)
    instruction: Optional[str] = None

    # When set, deliver the job result into this agent's active session channel
    report_to_agent: Optional[str] = None

    # When set, deliver the job result into this specific session (by session ID).
    # Takes precedence over report_to_agent. Used by the subagent system to pin
    # result delivery to the exact session that spawned the subagent.
    report_to_session: Optional[str] = None

    @model_validator(mode="after")
    def _resolve_preset(self) -> "AgentRun":
        """Resolve None include_* flags using the preset defaults."""
        defaults = PRESET_DEFAULTS[self.prompt_preset]
        for flag, default_val in defaults.items():
            field_name = f"include_{flag}"
            if getattr(self, field_name) is None:
                object.__setattr__(self, field_name, default_val)
        return self


JobRunType = Annotated[Union[CommandRun, AgentRun], Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Schedule types — when the job runs
# ---------------------------------------------------------------------------

class CronSchedule(BaseModel):
    """Standard 5-field cron expression."""
    kind: Literal["cron"] = "cron"
    expr: str
    timezone: Optional[str] = None  # None = use scheduler default (system local or config)
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

    # When False, this job is ephemeral — never written to jobs.yaml.
    # Used by the subagent system; may be useful for other transient jobs too.
    persistent: bool = True
    # Session ID of the agent session that spawned this subagent (None for regular jobs)
    spawned_by_session: Optional[str] = None

    # Metadata
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


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


# ---------------------------------------------------------------------------
# Per-agent YAML persistence (v2 format)
# ---------------------------------------------------------------------------

def save_agent_jobs(jobs: Dict[str, Job], agent_dir: Path) -> None:
    """Write jobs to ~/.pyclaw/agents/{id}/jobs.yaml (keyed by job name)."""
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.default_flow_style = False
        yaml.width = 120
    except ImportError:
        import logging
        logging.getLogger("pyclaw.jobs").error("ruamel.yaml not installed; cannot save agent jobs")
        return

    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "jobs.yaml"

    job_dict: Dict[str, Any] = {}
    for job in jobs.values():
        raw = job.model_dump(mode="json")
        # Name is the YAML key; remove it from the inner dict to avoid redundancy
        raw.pop("name", None)
        job_dict[job.name] = raw

    data: Dict[str, Any] = {"version": 2, "jobs": job_dict}
    tmp = path.with_suffix(".tmp")
    import io
    buf = io.StringIO()
    yaml.dump(data, buf)
    tmp.write_text(buf.getvalue())
    tmp.replace(path)


def load_agent_jobs(agent_dir: Path) -> Dict[str, Job]:
    """Load jobs from ~/.pyclaw/agents/{id}/jobs.yaml, returns {job_id: Job}."""
    path = agent_dir / "jobs.yaml"
    if not path.exists():
        return {}
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        raw_data = yaml.load(path.read_text())
    except ImportError:
        import logging
        logging.getLogger("pyclaw.jobs").error("ruamel.yaml not installed; cannot load agent jobs")
        return {}
    except Exception as e:
        import logging
        logging.getLogger("pyclaw.jobs").error(f"Error loading {path}: {e}")
        return {}

    if not raw_data or not isinstance(raw_data, dict):
        return {}

    jobs: Dict[str, Job] = {}
    for job_name, job_raw in raw_data.get("jobs", {}).items():
        if not isinstance(job_raw, dict):
            continue
        try:
            # Name is always derived from the YAML key (the authoritative source)
            job_raw = dict(job_raw)
            job_raw["name"] = job_name
            # ruamel may return CommentedMap — convert to plain dict recursively
            job_raw = _to_plain(job_raw)
            job = Job.model_validate(job_raw)
            jobs[job.id] = job
        except Exception as e:
            import logging
            logging.getLogger("pyclaw.jobs").warning(f"Skipping invalid job {job_name!r}: {e}")
    return jobs


def _to_plain(obj: Any) -> Any:
    """Recursively convert ruamel CommentedMap/Seq to plain dict/list."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj
