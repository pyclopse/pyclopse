"""Job management API routes."""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pyclaw.jobs.models import (
    AgentRun, AtSchedule, CommandRun, CronSchedule,
    DeliverAnnounce, DeliverNone, DeliverWebhook,
    FailureAlert, IntervalSchedule, Job, JobStatus,
)

logger = logging.getLogger("pyclaw.api.jobs")
router = APIRouter()


def _scheduler():
    """Get the live scheduler from the gateway."""
    from pyclaw.api.app import get_gateway
    gw = get_gateway()
    sched = getattr(gw, "_job_scheduler", None)
    if not sched:
        raise HTTPException(status_code=503, detail="Job scheduler not running")
    return sched


def _resolve(sched, name_or_id: str) -> Job:
    """Resolve job by name or ID, raise 404 if missing."""
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
        tz = parts[5] if len(parts) > 5 else "UTC"
        return CronSchedule(expr=expr, timezone=tz)

    raise ValueError(f"Cannot parse schedule: {s!r}")


def _parse_deliver(channel: Optional[str], chat_id: Optional[str], webhook_url: Optional[str]):
    if webhook_url:
        return DeliverWebhook(url=webhook_url)
    if channel is None and chat_id is None:
        return DeliverAnnounce()   # default: use gateway's active chat
    return DeliverAnnounce(channel=channel, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateCommandJobRequest(BaseModel):
    name: str
    schedule: str               # human-friendly: "0 9 * * *", "30m", ISO datetime
    command: str
    description: Optional[str] = None
    enabled: bool = True
    timeout_seconds: int = 300
    max_retries: int = 0
    delete_after_run: bool = False
    deliver_channel: Optional[str] = None
    deliver_chat_id: Optional[str] = None
    deliver_webhook_url: Optional[str] = None
    alert_after: Optional[int] = None
    owner: Optional[str] = None


class CreateAgentJobRequest(BaseModel):
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
    owner: Optional[str] = None


class UpdateJobRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    schedule: Optional[str] = None
    timeout_seconds: Optional[int] = None
    deliver_channel: Optional[str] = None
    deliver_chat_id: Optional[str] = None
    deliver_webhook_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def scheduler_status() -> Dict[str, Any]:
    """Overall scheduler status."""
    return _scheduler().get_status()


@router.get("/")
async def list_jobs(enabled_only: bool = False, owner: Optional[str] = None) -> Dict[str, Any]:
    """List all jobs, optionally filtered by owner."""
    jobs = await _scheduler().list_jobs(owner=owner)
    if enabled_only:
        jobs = [j for j in jobs if j.enabled]
    return {
        "jobs": [j.model_dump(mode="json") for j in jobs],
        "total": len(jobs),
    }


@router.get("/{name_or_id}")
async def get_job(name_or_id: str) -> Dict[str, Any]:
    """Get a job by name or ID."""
    job = _resolve(_scheduler(), name_or_id)
    return job.model_dump(mode="json")


@router.post("/command", status_code=201)
async def create_command_job(req: CreateCommandJobRequest) -> Dict[str, Any]:
    """Create a shell command job."""
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
        owner=req.owner,
    )
    await _scheduler().add_job(job)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.post("/agent", status_code=201)
async def create_agent_job(req: CreateAgentJobRequest) -> Dict[str, Any]:
    """Create an agent prompt job."""
    try:
        schedule = _parse_schedule(req.schedule)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job = Job(
        id=str(uuid.uuid4()),
        name=req.name,
        description=req.description,
        enabled=req.enabled,
        run=AgentRun(agent=req.agent, message=req.message, model=req.model),
        schedule=schedule,
        deliver=_parse_deliver(req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url),
        on_failure=FailureAlert(alert_after=req.alert_after) if req.alert_after else None,
        timeout_seconds=req.timeout_seconds,
        delete_after_run=req.delete_after_run,
        owner=req.owner,
    )
    await _scheduler().add_job(job)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.patch("/{name_or_id}")
async def update_job(name_or_id: str, req: UpdateJobRequest) -> Dict[str, Any]:
    """Update a job's configuration."""
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
    if any(v is not None for v in [req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url]):
        job.deliver = _parse_deliver(req.deliver_channel, req.deliver_chat_id, req.deliver_webhook_url)

    await sched.update_job(job)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.delete("/{name_or_id}")
async def delete_job(name_or_id: str) -> Dict[str, Any]:
    """Delete a job."""
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.remove_job(job.id)
    return {"ok": True, "deleted": job.name}


@router.post("/{name_or_id}/enable")
async def enable_job(name_or_id: str) -> Dict[str, Any]:
    """Enable a disabled job."""
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.enable_job(job.id)
    return {"ok": True, "job": job.name, "next_run": str(job.next_run)}


@router.post("/{name_or_id}/disable")
async def disable_job(name_or_id: str) -> Dict[str, Any]:
    """Disable a job without deleting it."""
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.disable_job(job.id)
    return {"ok": True, "job": job.name}


@router.post("/{name_or_id}/run")
async def run_job_now(name_or_id: str) -> Dict[str, Any]:
    """Trigger a job to run immediately."""
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    await sched.run_job_now(job.id)
    return {"ok": True, "job": job.name, "status": "triggered"}


@router.get("/{name_or_id}/history")
async def get_job_history(name_or_id: str, limit: int = 20) -> Dict[str, Any]:
    """Get recent run history for a job."""
    sched = _scheduler()
    job = _resolve(sched, name_or_id)
    runs = sched.get_run_history(job.id, limit=limit)
    return {
        "job_id": job.id,
        "job_name": job.name,
        "runs": [r.model_dump(mode="json") for r in runs],
    }
