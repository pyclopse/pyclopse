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
    """Execute a shell command as a job run type.

    Attributes:
        kind (str): Discriminator literal, always "command".
        command (str): The shell command string to execute.
    """

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
    """Send a message to a named agent and deliver its response as a job run type.

    Attributes:
        kind (str): Discriminator literal, always "agent".
        agent (str): Name of the target agent to send the message to.
        message (str): The message text to deliver to the agent.
        model (Optional[str]): Model override for this run; uses agent default if None.
        session_mode (str): Session continuity mode — "isolated" or "persistent".
        prompt_preset (str): Preset name controlling default include_* flag values.
        include_personality (Optional[bool]): Include personality section in system prompt.
        include_identity (Optional[bool]): Include identity section in system prompt.
        include_rules (Optional[bool]): Include rules section in system prompt.
        include_memory (Optional[bool]): Include memory content in system prompt.
        include_user (Optional[bool]): Include user profile in system prompt.
        include_agents (Optional[bool]): Include agents listing in system prompt.
        include_tools (Optional[bool]): Include tools listing in system prompt.
        include_skills (Optional[bool]): Include skills in system prompt.
        skills (Optional[List[str]]): Named skills to inject; None means all discovered skills.
        include_files (Optional[List[str]]): File paths whose contents are appended to the prompt.
        instruction (Optional[str]): Instruction appended to the system prompt.
        report_to_agent (Optional[str]): Agent whose active session channel receives the result.
        report_to_session (Optional[str]): Specific session ID to deliver the result to.
    """

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
        """Resolve None include_* flags using the preset defaults.

        For each include_* field that is None, this validator substitutes the
        corresponding value from PRESET_DEFAULTS for the chosen prompt_preset.

        Returns:
            AgentRun: The model instance with all include_* flags resolved.
        """
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
    """Standard 5-field cron expression schedule.

    Attributes:
        kind (str): Discriminator literal, always "cron".
        expr (str): A 5-field cron expression (e.g., "0 9 * * 1-5"). The
            special leading token "continuous" is also supported.
        timezone (Optional[str]): IANA timezone name for cron evaluation.
            None means use the scheduler's default (system local or config).
        stagger_seconds (int): Random jitter added to each next_run, up to
            this many seconds. Defaults to 0.
    """

    kind: Literal["cron"] = "cron"
    expr: str
    timezone: Optional[str] = None  # None = use scheduler default (system local or config)
    stagger_seconds: int = 0      # random jitter up to N seconds


class IntervalSchedule(BaseModel):
    """Fixed interval between runs schedule.

    Attributes:
        kind (str): Discriminator literal, always "interval".
        seconds (int): Number of seconds between consecutive runs.
    """

    kind: Literal["interval"] = "interval"
    seconds: int


class AtSchedule(BaseModel):
    """One-shot schedule: run once at an absolute datetime then delete or disable.

    Attributes:
        kind (str): Discriminator literal, always "at".
        at (datetime): The absolute datetime at which the job should fire once.
    """

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
    """Suppress job output — run silently without delivering results anywhere.

    Attributes:
        mode (str): Discriminator literal, always "none".
    """

    mode: Literal["none"] = "none"


class DeliverAnnounce(BaseModel):
    """Deliver job result to a messaging channel such as Telegram or Slack.

    Attributes:
        mode (str): Discriminator literal, always "announce".
        channel (Optional[str]): Channel type to deliver to (e.g., "telegram", "slack").
            None lets the gateway pick a default.
        chat_id (Optional[str]): Channel-specific recipient identifier (e.g., a chat or
            user ID). None lets the gateway pick a default.
    """

    mode: Literal["announce"] = "announce"
    channel: Optional[str] = None    # "telegram", "slack", etc.
    chat_id: Optional[str] = None    # channel-specific recipient ID


class DeliverWebhook(BaseModel):
    """HTTP POST the job result to a URL as a delivery mechanism.

    Attributes:
        mode (str): Discriminator literal, always "webhook".
        url (str): Destination URL that receives the POST request with the job result.
    """

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
    """Alert configuration triggered when a job fails consecutively.

    Attributes:
        alert_after (int): Number of consecutive errors before an alert is sent.
            Defaults to 3.
        channel (Optional[str]): Channel type to send the alert to (e.g., "telegram").
            None lets the gateway choose.
        chat_id (Optional[str]): Channel-specific recipient ID for the alert.
            None lets the gateway choose.
    """

    alert_after: int = 3            # consecutive errors before alerting
    channel: Optional[str] = None
    chat_id: Optional[str] = None


class JobStatus(str, Enum):
    """Enumeration of possible job lifecycle states.

    Attributes:
        PENDING: Job is scheduled and waiting for its next_run time.
        RUNNING: Job is currently executing.
        COMPLETED: Job's most recent execution finished successfully.
        FAILED: Job's most recent execution ended with an error or non-zero exit.
        DISABLED: Job has been administratively disabled and will not run.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Job — the main model
# ---------------------------------------------------------------------------

class Job(BaseModel):
    """A scheduled job definition plus its runtime state.

    Combines the static configuration (what to run, when to run, delivery
    settings) with the mutable runtime state that the scheduler maintains
    (last_run, run_count, status, next_run, etc.).

    Attributes:
        id (str): Unique job identifier (UUID string).
        name (str): Human-readable job name used as the YAML key.
        description (Optional[str]): Optional free-form description.
        enabled (bool): Whether the job participates in scheduling. Defaults to True.
        tags (List[str]): Arbitrary tags for filtering and organisation.
        run (JobRunType): Discriminated union describing what to execute.
        schedule (JobScheduleType): Discriminated union describing when to run.
        deliver (DeliveryType): Where and how to deliver results. Defaults to DeliverAnnounce.
        on_failure (Optional[FailureAlert]): Alert settings for repeated failures.
        timeout_seconds (int): Maximum execution time before the job is killed. Defaults to 300.
        max_retries (int): Number of automatic retries on failure. Defaults to 0.
        delete_after_run (bool): If True, one-shot AtSchedule jobs delete themselves on success.
        status (JobStatus): Current lifecycle state. Defaults to PENDING.
        next_run (Optional[datetime]): Scheduled next execution time.
        last_run (Optional[datetime]): Timestamp of the most recent execution start.
        last_result (Optional[Dict[str, Any]]): Summary dict from the most recent run.
        run_count (int): Total number of times the job has executed.
        failure_count (int): Total number of failed executions.
        consecutive_errors (int): Number of consecutive failures since the last success.
        persistent (bool): If False, job is ephemeral and never written to jobs.yaml.
        spawned_by_session (Optional[str]): Session ID of the agent that spawned this
            subagent job; None for regular scheduled jobs.
        created_at (datetime): Timestamp when the job was created.
        updated_at (datetime): Timestamp of the last state modification.
    """

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
    """Record of one job execution, written to a per-job JSONL log file.

    Attributes:
        id (str): Unique run identifier (UUID string).
        job_id (str): ID of the parent Job.
        job_name (str): Snapshot of the job name at run time.
        started_at (datetime): Timestamp when execution began.
        ended_at (Optional[datetime]): Timestamp when execution finished; None while running.
        status (JobStatus): Final status of this run. Defaults to RUNNING.
        stdout (str): Captured standard output from the process. Defaults to "".
        stderr (str): Captured standard error from the process. Defaults to "".
        exit_code (Optional[int]): Process exit code; None for agent runs or timed-out jobs.
        error (Optional[str]): Human-readable error message if the run failed.
    """

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
        """Return the run duration in milliseconds, or None if the run has not ended.

        Returns:
            Optional[float]: Elapsed milliseconds between started_at and ended_at,
                or None if ended_at is not yet set.
        """
        if self.ended_at and self.started_at:
            return (self.ended_at - self.started_at).total_seconds() * 1000
        return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_jobs(jobs: Dict[str, Job], path: Path) -> None:
    """Atomically write jobs to a JSON file using a write-then-rename strategy.

    Creates parent directories if they do not exist. The file is written to a
    temporary path first and then atomically renamed to ``path`` so that a
    partial write never corrupts the existing data.

    Args:
        jobs (Dict[str, Job]): Mapping of job ID to Job instance to persist.
        path (Path): Destination file path for the JSON output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 2,
        "jobs": [job.model_dump(mode="json") for job in jobs.values()],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def load_jobs(path: Path) -> Dict[str, Job]:
    """Load jobs from a JSON file, migrating v1 flat format to v2 nested format if needed.

    Invalid job entries are skipped with a warning rather than raising an error.

    Args:
        path (Path): Path to the JSON file to load.

    Returns:
        Dict[str, Job]: Mapping of job ID to Job instance. Returns an empty dict
            if the file does not exist or cannot be parsed.
    """
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
    """Migrate a v1 flat job record to the v2 nested format.

    Constructs ``run``, ``schedule``, and ``deliver`` sub-objects from
    top-level v1 fields and removes obsolete keys. Does not raise; returns
    a best-effort converted dict.

    Args:
        raw (Dict[str, Any]): Raw v1 job dict read from disk.

    Returns:
        Dict[str, Any]: Equivalent v2 job dict ready for Pydantic validation.
    """
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
    """Append a completed run record to the per-job JSONL log file.

    Creates the runs directory and the log file if they do not exist.
    Each call appends a single JSON line to ``{runs_dir}/{run.job_id}.jsonl``.

    Args:
        run (JobRun): The completed run record to persist.
        runs_dir (Path): Directory that holds per-job JSONL log files.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{run.job_id}.jsonl"
    with open(log_path, "a") as f:
        f.write(run.model_dump_json() + "\n")


def read_run_log(job_id: str, runs_dir: Path, limit: int = 20) -> List[JobRun]:
    """Read the last N run records for a job from its JSONL log file.

    Lines that cannot be parsed are silently skipped.

    Args:
        job_id (str): ID of the job whose run history is requested.
        runs_dir (Path): Directory containing per-job JSONL log files.
        limit (int): Maximum number of most-recent runs to return. Defaults to 20.

    Returns:
        List[JobRun]: Most-recent run records in chronological order (oldest first
            within the returned window). Returns an empty list if no log exists.
    """
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
    """Write jobs to ``~/.pyclaw/agents/{id}/jobs.yaml`` keyed by job name.

    Uses ruamel.yaml for output and atomically replaces the file via a
    write-then-rename strategy. The job's name becomes the YAML mapping key
    and is omitted from the inner dict to avoid redundancy.

    Args:
        jobs (Dict[str, Job]): Mapping of job ID to Job instance to persist.
        agent_dir (Path): Agent directory (e.g., ``~/.pyclaw/agents/assistant``).
            Created if it does not exist.
    """
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
    """Load jobs from ``~/.pyclaw/agents/{id}/jobs.yaml``.

    The YAML file uses job names as keys. Each job's name is authoritative and
    is injected from the YAML key rather than the inner dict. ruamel CommentedMap
    objects are recursively converted to plain dicts before Pydantic validation.
    Invalid entries are skipped with a warning.

    Args:
        agent_dir (Path): Agent directory containing a ``jobs.yaml`` file.

    Returns:
        Dict[str, Job]: Mapping of job ID to Job instance. Returns an empty dict
            if the file does not exist or cannot be parsed.
    """
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
    """Recursively convert ruamel CommentedMap/Seq objects to plain dict/list.

    This is needed because ruamel.yaml returns CommentedMap and CommentedSeq
    instances that are subclasses of dict and list but not accepted by Pydantic
    as plain mappings in all situations.

    Args:
        obj (Any): The object to convert. Dicts and lists are processed
            recursively; all other types are returned unchanged.

    Returns:
        Any: A plain Python dict, list, or scalar equivalent of ``obj``.
    """
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj
