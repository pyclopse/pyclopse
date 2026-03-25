"""REST API routes for the subagent system."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("pyclawops.api.subagents")


def _get_gateway():
    """Retrieve the global gateway instance.

    Returns:
        Any: The gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclawops.api.app import get_gateway
    return get_gateway()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SpawnSubagentRequest(BaseModel):
    """Request body for spawning a background subagent.

    Attributes:
        agent (str): ID of the agent to run as a subagent.
        task (str): Task description / prompt sent to the subagent.
        model (Optional[str]): Model override for this subagent run.
        timeout_seconds (int): Execution timeout in seconds. Defaults to 300.
        prompt_preset (str): System prompt preset ("minimal", "full", etc.).
            Defaults to "minimal".
        instruction (Optional[str]): Additional instruction injected into the
            system prompt.
        spawned_by_session (Optional[str]): Session ID of the spawning agent.
            When omitted, the agent's active session is resolved automatically.
    """

    agent: str
    task: str
    model: Optional[str] = None
    timeout_seconds: int = 300
    prompt_preset: str = "minimal"
    instruction: Optional[str] = None
    # If omitted, the endpoint resolves the agent's current active session.
    spawned_by_session: Optional[str] = None


class InterruptSubagentRequest(BaseModel):
    """Request body for interrupting a subagent with a new task.

    Attributes:
        task (str): New task description to use when respawning the subagent.
    """

    task: str


class SendSubagentRequest(BaseModel):
    """Request body for queueing a follow-up message for a running subagent.

    Attributes:
        message (str): Follow-up message text to queue for the subagent.
    """

    message: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/")
async def spawn_subagent(req: SpawnSubagentRequest):
    """Spawn a background subagent. Returns job_id and the pinned session_id."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    # Resolve spawning session: use provided ID or fall back to agent's active session
    session_id = req.spawned_by_session
    if not session_id and gw._session_manager:
        active = await gw._session_manager.get_active_session(req.agent)
        if active:
            session_id = active.id

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail=f"No active session for agent '{req.agent}' — session ID required",
        )

    job_id = await scheduler.spawn_subagent(
        task=req.task,
        agent=req.agent,
        spawned_by_session=session_id,
        model=req.model,
        timeout_seconds=req.timeout_seconds,
        prompt_preset=req.prompt_preset,
        instruction=req.instruction,
    )

    return {
        "job_id": job_id,
        "session_id": session_id,
        "name": f"subagent-{job_id[:8]}",
        "agent": req.agent,
        "task": req.task,
    }


@router.get("/")
async def list_subagents(agent: Optional[str] = None):
    """List all in-memory subagent jobs, optionally filtered by owning agent."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    entries = scheduler.list_subagents(spawned_by_agent=agent)
    result = []
    for job, sess_id in entries:
        result.append({
            "job_id": job.id,
            "name": job.name,
            "agent": getattr(job.run, "agent", None),
            "task": getattr(job.run, "message", None),
            "status": job.status.value,
            "spawned_by_session": job.spawned_by_session,
            "session_id": sess_id,
            "started_at": job.last_run.isoformat() if job.last_run else None,
            "run_count": job.run_count,
        })

    return {"subagents": result, "total": len(result)}


@router.get("/{job_id}")
async def get_subagent(job_id: str):
    """Get status and details of a single subagent job."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    job = scheduler.jobs.get(job_id)
    if not job or job.spawned_by_session is None:
        raise HTTPException(status_code=404, detail=f"Subagent '{job_id}' not found")

    sess_id = scheduler._subagent_sessions.get(job_id, "")
    return {
        "job_id": job.id,
        "name": job.name,
        "agent": getattr(job.run, "agent", None),
        "task": getattr(job.run, "message", None),
        "status": job.status.value,
        "spawned_by_session": job.spawned_by_session,
        "session_id": sess_id,
        "started_at": job.last_run.isoformat() if job.last_run else None,
        "run_count": job.run_count,
        "queued_messages": len(scheduler._subagent_message_queue.get(job_id, [])),
    }


@router.delete("/{job_id}")
async def kill_subagent(job_id: str):
    """Cancel a running subagent."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    killed = await scheduler.kill_subagent(job_id)
    if not killed:
        raise HTTPException(
            status_code=404,
            detail=f"Subagent '{job_id}' not found or not running",
        )
    return {"killed": True, "job_id": job_id}


@router.post("/{job_id}/interrupt")
async def interrupt_subagent(job_id: str, req: InterruptSubagentRequest):
    """Kill the running subagent and respawn it with a new task."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    job = scheduler.jobs.get(job_id)
    if not job or job.spawned_by_session is None:
        raise HTTPException(status_code=404, detail=f"Subagent '{job_id}' not found")

    # Capture metadata before killing
    old_run = job.run
    spawned_by = job.spawned_by_session or ""
    agent = getattr(old_run, "agent", "")
    model = getattr(old_run, "model", None)
    timeout = job.timeout_seconds
    preset = getattr(old_run, "prompt_preset", "minimal")
    instruction = getattr(old_run, "instruction", None)

    await scheduler.kill_subagent(job_id)

    # Respawn with the new task
    new_job_id = await scheduler.spawn_subagent(
        task=req.task,
        agent=agent,
        spawned_by_session=spawned_by,
        model=model,
        timeout_seconds=timeout,
        prompt_preset=preset,
        instruction=instruction,
    )

    return {
        "interrupted": True,
        "old_job_id": job_id,
        "new_job_id": new_job_id,
        "task": req.task,
    }


@router.post("/{job_id}/send")
async def send_to_subagent(job_id: str, req: SendSubagentRequest):
    """Queue a follow-up message for a running subagent."""
    gw = _get_gateway()
    scheduler = getattr(gw, "_job_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Job scheduler not available")

    queued = scheduler.queue_message(job_id, req.message)
    if not queued:
        raise HTTPException(
            status_code=404,
            detail=f"Subagent '{job_id}' not found — may have already completed",
        )
    return {"queued": True, "job_id": job_id}
