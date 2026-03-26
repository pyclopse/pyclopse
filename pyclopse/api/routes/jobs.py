"""Job management API routes."""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pyclopse.jobs.models import (
    AgentRun, AtSchedule, CommandRun, CronSchedule,
    DeliverAnnounce, DeliverNone, DeliverWebhook,
    FailureAlert, IntervalSchedule, Job, JobStatus,
)

logger = logging.getLogger("pyclopse.api.jobs")
router = APIRouter()


def _scheduler():
    """Get the live job scheduler from the gateway.

    Returns:
        Any: The running job scheduler instance.

    Raises:
        HTTPException: With status 503 if the scheduler is not running.
    """
    from pyclopse.api.app import get_gateway
    gw = get_gateway()
    sched = getattr(gw, "_job_scheduler", None)
    if not sched:
        raise HTTPException(status_code=503, detail="Job scheduler not running")
    return sched


def _resolve(sched, name_or_id: str) -> Job:
    """Resolve a job from the scheduler by name or ID.

    Args:
        sched: The active job scheduler instance.
        name_or_id (str): Either the job's UUID or its human-readable name.

    Returns:
        Job: The matching job object.

    Raises:
        HTTPException: With status 404 if no job matches ``name_or_id``.
    """
    job = sched.resolve(name_or_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {name_or_id!r}")
    return job


def _parse_schedule(schedule_str: str):
    """
    Parse a human-friendly schedule string into a schedule object.

    Accepted formats:
      "0 9 * * *"           → CronSchedule
      "0 9 * * * America/New_York"  → CronSchedule with timezone
      "30m" / "1h" / "2d"  → IntervalSchedule
      "2026-03-10T09:00:00Z" → AtSchedule (one-shot)
    """
    s = schedule_str.strip()

    # Interval shorthand: 30m, 2h, 7d
    if s and s[-1] in ("s", "m", "h", "d") and s[:-1].isdigit():
        n = int(s[:-1])
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[s[-1]]
        return IntervalSchedule(seconds=n * mult)

    # ISO datetime → one-shot
    try:
        at = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return AtSchedule(at=at.replace(tzinfo=None))
    except ValueError:
        pass

    # Cron expression (5 or 6 fields, optional trailing timezone)
    parts = s.split()
    if len(parts) >= 5:
        expr = " ".join(parts[:5])
        tz = parts[5] if len(parts) > 5 else None
        return CronSchedule(expr=expr, timezone=tz)

    raise ValueError(f"Cannot parse schedule: {s!r}")


def _parse_deliver(channel: Optional[str], chat_id: Optional[str], webhook_url: Optional[str]):
    """Build a delivery target object from the supplied parameters.

    Args:
        channel (Optional[str]): Channel name for the delivery (e.g. "telegram").
        chat_id (Optional[str]): Specific chat / user ID within the channel.
        webhook_url (Optional[str]): External webhook URL.  Takes priority over
            channel/chat_id when provided.

    Returns:
        DeliverWebhook | DeliverAnnounce: The appropriate delivery target.
    """
    if webhook_url:
        return DeliverWebhook(url=webhook_url)
    if channel is None and chat_id is None:
        return DeliverAnnounce()   # default: use gateway's active chat
    return DeliverAnnounce(channel=channel, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateCommandJobRequest(BaseModel):
    """Request body for creating a shell-command job.

    Attributes:
        name (str): Unique human-readable job name.
        schedule (str): Schedule string — cron expression, interval shorthand
            (e.g. "30m"), or ISO datetime for a one-shot run.
        command (str): Shell command to execute.
        agent (Optional[str]): Agent that owns this job; determines which
            agent's jobs.yaml file the job is written to.
        description (Optional[str]): Human-readable job description.
        enabled (bool): Whether the job starts enabled. Defaults to True.
        timeout_seconds (int): Execution timeout in seconds. Defaults to 300.
        max_retries (int): Number of retries on failure. Defaults to 0.
        delete_after_run (bool): Remove job after it runs once. Defaults to False.
        deliver_channel (Optional[str]): Target channel for job output delivery.
        deliver_chat_id (Optional[str]): Target chat ID for output delivery.
        deliver_webhook_url (Optional[str]): Webhook URL for output delivery.
        alert_after (Optional[int]): Failure-alert threshold in minutes.
    """

    name: str
    schedule: str               # human-friendly: "0 9 * * *", "30m", ISO datetime
    command: str
    agent: Optional[str] = None  # agent that owns this job (determines which jobs.yaml to write)
    description: Optional[str] = None
    enabled: bool = True
    timeout_seconds: int = 300
    max_retries: int = 0
    delete_after_run: bool = False
    deliver_channel: Optional[str] = None
    deliver_chat_id: Optional[str] = None
    deliver_webhook_url: Optional[str] = None
    alert_after: Optional[int] = None


class CreateAgentJobRequest(BaseModel):
    """Request body for creating an agent-prompt job.

    Attributes:
        name (str): Unique human-readable job name.
        schedule (str): Schedule string (cron, interval, or ISO datetime).
        agent (str): Agent ID that will process the message.
        message (str): Prompt text sent to the agent on each run.
        model (Optional[str]): Model override for this job's runs.
        description (Optional[str]): Human-readable job description.
        enabled (bool): Whether the job starts enabled. Defaults to True.
        timeout_seconds (int): Execution timeout in seconds. Defaults to 300.
        delete_after_run (bool): Remove job after it runs once. Defaults to False.
        deliver_channel (Optional[str]): Target channel for output delivery.
        deliver_chat_id (Optional[str]): Target chat ID for output delivery.
        deliver_webhook_url (Optional[str]): Webhook URL for output delivery.
        alert_after (Optional[int]): Failure-alert threshold in minutes.
        report_to_agent (Optional[str]): Agent ID that receives the run report.
    """

    name: str
    schedule: str
    agent: str
    message: str
    model: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True
    timeout_seconds: int = 300
    delete_after_run: bool = False
    deliver_channel: Optional[str] = None
    deliver_chat_id: Optional[str] = None
    deliver_webhook_url: Optional[str] = None
    alert_after: Optional[int] = None
    report_to_agent: Optional[str] = None


class UpdateJobRequest(BaseModel):
    """Partial update payload for an existing job.

    All fields are optional. Only the fields explicitly set in the request
    body are applied to the job. For AgentRun jobs, prompt/session fields
    are applied only when the job's ``run.kind`` is "agent".

    Attributes:
        name (Optional[str]): New job name.
        description (Optional[str]): New description.
        enabled (Optional[bool]): Enable or disable the job.
        schedule (Optional[str]): New schedule expression.
        timeout_seconds (Optional[int]): New execution timeout.
        deliver_channel (Optional[str]): New delivery channel.
        deliver_chat_id (Optional[str]): New delivery chat ID.
        deliver_webhook_url (Optional[str]): New delivery webhook URL.
        deliver_none (bool): When True, set delivery to DeliverNone (discard output).
        session_mode (Optional[str]): AgentRun session mode override.
        report_to_agent (Optional[str]): Agent that receives the run report.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    schedule: Optional[str] = None
    timeout_seconds: Optional[int] = None
    deliver_channel: Optional[str] = None
    deliver_chat_id: Optional[str] = None
    deliver_webhook_url: Optional[str] = None
    # AgentRun prompt/session fields (only applied when job.run.kind == "agent")
    session_mode: Optional[str] = None
    prompt_preset: Optional[str] = None
    include_personality: Optional[bool] = None
    include_identity: Optional[bool] = None
    include_rules: Optional[bool] = None
    include_memory: Optional[bool] = None
    include_user: Optional[bool] = None
    include_agents: Optional[bool] = None
    include_tools: Optional[bool] = None
    include_skills: Optional[bool] = None
    instruction: Optional[str] = None
    report_to_agent: Optional[str] = None
    deliver_none: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def scheduler_status() -> Dict[str, Any]:
    """Return the job scheduler's overall status summary.

    Returns:
        Dict[str, Any]: Scheduler status data (running, job counts, etc.).
    """
    return _scheduler().get_status()


@router.get("/")
async def list_jobs(enabled_only: bool = False, owner: Optional[str] = None) -> Dict[str, Any]:
    """List all scheduled jobs.

    Args:
        enabled_only (bool): When True, return only enabled jobs. Defaults to False.
        owner (Optional[str]): Filter jobs to those owned by this agent ID.

    Returns:
        Dict[str, Any]: ``{"jobs": [...], "total": int}`` where each job is
            serialised via ``model_dump(mode="json")``.
    """
    jobs = await _scheduler().list_jobs(owner=owner)
    if enabled_only:
        jobs = [j for j in jobs if j.enabled]
    return {
        "jobs": [j.model_dump(mode="json") for j in jobs],
        "total": len(jobs),
    }


@router.get("/{name_or_id}")
async def get_job(name_or_id: str) -> Dict[str, Any]:
    """Return a single job by name or UUID.

    Args:
        name_or_id (str): The job's UUID or human-readable name.

    Returns:
        Dict[str, Any]: The job serialised via ``model_dump(mode="json")``.

    Raises:
        HTTPException: With status 404 if the job is not found.
    """
    job = _resolve(_scheduler(), name_or_id)
    return job.model_dump(mode="json")


@router.post("/command", status_code=201)
async def create_command_job(req: CreateCommandJobRequest) -> Dict[str, Any]:
    """Create and schedule a new shell-command job.

    Args:
        req (CreateCommandJobRequest): Job configuration including name,
            schedule, and command to execute.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": {...}}`` with the created job.

    Raises:
        HTTPException: With status 422 if the schedule string cannot be parsed.
    """
    try:
        schedule = _parse_schedule(req.schedule)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job = Job(
        id=str(uuid.uuid4()),
        name=req.name,
        description=req.description,
        enabled=req.enabled,
        run=CommandRun(command=req.command),
        schedule=schedule,
        deliver=_parse_deliver(req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url),
        on_failure=FailureAlert(alert_after=req.alert_after) if req.alert_after else None,
        timeout_seconds=req.timeout_seconds,
        max_retries=req.max_retries,
        delete_after_run=req.delete_after_run,
    )
    await _scheduler().add_job(job, agent_id=req.agent)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.post("/agent", status_code=201)
async def create_agent_job(req: CreateAgentJobRequest) -> Dict[str, Any]:
    """Create and schedule a new agent-prompt job.

    Args:
        req (CreateAgentJobRequest): Job configuration including agent ID,
            message prompt, and schedule.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": {...}}`` with the created job.

    Raises:
        HTTPException: With status 422 if the schedule string cannot be parsed.
    """
    try:
        schedule = _parse_schedule(req.schedule)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job = Job(
        id=str(uuid.uuid4()),
        name=req.name,
        description=req.description,
        enabled=req.enabled,
        run=AgentRun(agent=req.agent, message=req.message, model=req.model,
                     report_to_agent=req.report_to_agent),
        schedule=schedule,
        deliver=_parse_deliver(req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url),
        on_failure=FailureAlert(alert_after=req.alert_after) if req.alert_after else None,
        timeout_seconds=req.timeout_seconds,
        delete_after_run=req.delete_after_run,
    )
    await _scheduler().add_job(job)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.patch("/{name_or_id}")
async def update_job(name_or_id: str, req: UpdateJobRequest) -> Dict[str, Any]:
    """Partially update a job's configuration.

    Args:
        name_or_id (str): The job's UUID or human-readable name.
        req (UpdateJobRequest): Fields to update; None values are skipped.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": {...}}`` with the updated job.

    Raises:
        HTTPException: 404 if the job is not found; 422 if the new schedule
            string cannot be parsed.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)

    if req.name is not None:
        job.name = req.name
    if req.description is not None:
        job.description = req.description
    if req.enabled is not None:
        job.enabled = req.enabled
    if req.timeout_seconds is not None:
        job.timeout_seconds = req.timeout_seconds
    if req.schedule is not None:
        try:
            job.schedule = _parse_schedule(req.schedule)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if req.deliver_none:
        job.deliver = DeliverNone()
    elif any(v is not None for v in [req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url]):
        job.deliver = _parse_deliver(req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url)

    # Apply AgentRun prompt/session fields if this is an agent job
    _agent_run_fields = {
        "session_mode": req.session_mode,
        "prompt_preset": req.prompt_preset,
        "include_personality": req.include_personality,
        "include_identity": req.include_identity,
        "include_rules": req.include_rules,
        "include_memory": req.include_memory,
        "include_user": req.include_user,
        "include_agents": req.include_agents,
        "include_tools": req.include_tools,
        "include_skills": req.include_skills,
        "instruction": req.instruction,
        "report_to_agent": req.report_to_agent,
    }
    if any(v is not None for v in _agent_run_fields.values()) and getattr(job.run, "kind", None) == "agent":
        current = job.run.model_dump()
        current.update({k: v for k, v in _agent_run_fields.items() if v is not None})
        job.run = AgentRun.model_validate(current)

    await sched.update_job(job)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.delete("/{name_or_id}")
async def delete_job(name_or_id: str) -> Dict[str, Any]:
    """Delete a job permanently.

    System jobs (names wrapped in double underscores, e.g. ``__heartbeat__``)
    cannot be deleted; use ``/disable`` to turn them off instead.

    Args:
        name_or_id (str): The job's UUID or human-readable name.

    Returns:
        Dict[str, Any]: ``{"ok": True, "deleted": "<job_name>"}``.

    Raises:
        HTTPException: 404 if the job is not found; 403 for system jobs.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    if job.name.startswith("__") and job.name.endswith("__"):
        raise HTTPException(status_code=403, detail=f"System job '{job.name}' cannot be deleted. Use enable/disable to control it.")
    await sched.remove_job(job.id)
    return {"ok": True, "deleted": job.name}


@router.post("/{name_or_id}/enable")
async def enable_job(name_or_id: str) -> Dict[str, Any]:
    """Enable a previously disabled job.

    Args:
        name_or_id (str): The job's UUID or human-readable name.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": "<name>", "next_run": "..."}``.

    Raises:
        HTTPException: With status 404 if the job is not found.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.enable_job(job.id)
    return {"ok": True, "job": job.name, "next_run": str(job.next_run)}


@router.post("/{name_or_id}/disable")
async def disable_job(name_or_id: str) -> Dict[str, Any]:
    """Disable a job so it will not run until re-enabled.

    The job remains in the scheduler and can be re-enabled via ``/enable``.

    Args:
        name_or_id (str): The job's UUID or human-readable name.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": "<name>"}``.

    Raises:
        HTTPException: With status 404 if the job is not found.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.disable_job(job.id)
    return {"ok": True, "job": job.name}


@router.post("/{name_or_id}/run")
async def run_job_now(name_or_id: str) -> Dict[str, Any]:
    """Trigger an immediate out-of-schedule run for a job.

    The job is dispatched asynchronously; the response is returned before
    execution completes.

    Args:
        name_or_id (str): The job's UUID or human-readable name.

    Returns:
        Dict[str, Any]: ``{"ok": True, "job": "<name>", "status": "triggered"}``.

    Raises:
        HTTPException: With status 404 if the job is not found.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.run_job_now(job.id)
    return {"ok": True, "job": job.name, "status": "triggered"}


@router.get("/{name_or_id}/history")
async def get_job_history(name_or_id: str, limit: int = 20) -> Dict[str, Any]:
    """Return recent execution history for a job.

    Args:
        name_or_id (str): The job's UUID or human-readable name.
        limit (int): Maximum number of run records to return. Defaults to 20.

    Returns:
        Dict[str, Any]: ``{"job_id": ..., "job_name": ..., "runs": [...]}``
            where each run includes its duration in milliseconds.

    Raises:
        HTTPException: With status 404 if the job is not found.
    """
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    runs = sched.get_run_history(job.id, limit=limit)
    return {
        "job_id": job.id,
        "job_name": job.name,
        "runs": [{**r.model_dump(mode="json"), "duration_ms": r.duration_ms()} for r in runs],
    }
