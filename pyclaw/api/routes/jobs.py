"""Job management API routes."""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("pyclaw.api.jobs")

router = APIRouter()


# Request/Response models
class JobCreate(BaseModel):
    """Job creation request."""
    name: str
    agent_id: str
    schedule: str  # Cron expression
    command: str
    enabled: bool = True


class JobUpdate(BaseModel):
    """Job update request."""
    name: Optional[str] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None
    command: Optional[str] = None


class JobResponse(BaseModel):
    """Job information response."""
    id: str
    name: str
    agent_id: str
    schedule: str
    command: str
    enabled: bool
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    status: str


class JobRunResponse(BaseModel):
    """Response from running a job."""
    job_id: str
    execution_id: str
    status: str


# Helper dependency
def get_gateway():
    """Get the gateway instance."""
    from pyclaw.api.app import get_gateway as _get_gateway
    return _get_gateway()


# List all jobs
@router.get("/", response_model=Dict[str, Any])
async def list_jobs(enabled_only: bool = False):
    """List all jobs."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or not gateway.jobs:
            return {"jobs": []}
        
        jobs = []
        for job_id, job in gateway.jobs.items():
            if enabled_only and not job.get("enabled", True):
                continue
            
            jobs.append({
                "id": job_id,
                "name": job.get("name", job_id),
                "agent_id": job.get("agent_id", "default"),
                "schedule": job.get("schedule", ""),
                "command": job.get("command", ""),
                "enabled": job.get("enabled", True),
                "last_run": job.get("last_run"),
                "next_run": job.get("next_run"),
                "status": job.get("status", "idle"),
            })
        
        return {"jobs": jobs}
    
    except Exception as e:
        logger.error(f"Error listing jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Create a new job
@router.post("/", response_model=JobResponse)
async def create_job(job: JobCreate):
    """Create a new job."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs'):
            raise HTTPException(status_code=500, detail="Jobs system not initialized")
        
        # Generate job ID
        job_id = f"job-{datetime.now().timestamp()}"
        
        # Create job
        job_data = {
            "id": job_id,
            "name": job.name,
            "agent_id": job.agent_id,
            "schedule": job.schedule,
            "command": job.command,
            "enabled": job.enabled,
            "status": "created",
        }
        
        if hasattr(gateway.jobs, 'create_job'):
            await gateway.jobs.create_job(job_data)
        else:
            gateway.jobs[job_id] = job_data
        
        return JobResponse(
            id=job_id,
            name=job.name,
            agent_id=job.agent_id,
            schedule=job.schedule,
            command=job.command,
            enabled=job.enabled,
            status="created",
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Get job info
@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Get information about a specific job."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or job_id not in gateway.jobs:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        job = gateway.jobs[job_id]
        
        return JobResponse(
            id=job_id,
            name=job.get("name", job_id),
            agent_id=job.get("agent_id", "default"),
            schedule=job.get("schedule", ""),
            command=job.get("command", ""),
            enabled=job.get("enabled", True),
            last_run=job.get("last_run"),
            next_run=job.get("next_run"),
            status=job.get("status", "idle"),
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Update a job
@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(job_id: str, update: JobUpdate):
    """Update a job's configuration."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or job_id not in gateway.jobs:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        job = gateway.jobs[job_id]
        
        # Apply updates
        if update.name is not None:
            job["name"] = update.name
        if update.schedule is not None:
            job["schedule"] = update.schedule
        if update.enabled is not None:
            job["enabled"] = update.enabled
        if update.command is not None:
            job["command"] = update.command
        
        # Save job if there's a save method
        if hasattr(gateway.jobs, 'save'):
            await gateway.jobs.save()
        
        return JobResponse(
            id=job_id,
            name=job.get("name", job_id),
            agent_id=job.get("agent_id", "default"),
            schedule=job.get("schedule", ""),
            command=job.get("command", ""),
            enabled=job.get("enabled", True),
            last_run=job.get("last_run"),
            next_run=job.get("next_run"),
            status=job.get("status", "idle"),
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Delete a job
@router.delete("/{job_id}")
async def delete_job(job_id: str):
    """Delete a job."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or job_id not in gateway.jobs:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        # Delete job
        if hasattr(gateway.jobs, 'delete_job'):
            await gateway.jobs.delete_job(job_id)
        else:
            del gateway.jobs[job_id]
        
        return {"ok": True, "message": f"Job '{job_id}' deleted"}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Run a job immediately
@router.post("/{job_id}/run", response_model=JobRunResponse)
async def run_job(job_id: str):
    """Run a job immediately."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or job_id not in gateway.jobs:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        job = gateway.jobs[job_id]
        
        # Generate execution ID
        execution_id = f"exec-{datetime.now().timestamp()}"
        
        # Run job
        if hasattr(gateway.jobs, 'run_job'):
            await gateway.jobs.run_job(job_id, execution_id)
        else:
            # Stub execution
            job["status"] = "running"
            job["last_run"] = datetime.now().isoformat()
        
        return JobRunResponse(
            job_id=job_id,
            execution_id=execution_id,
            status="started",
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Get job execution history
@router.get("/{job_id}/history", response_model=Dict[str, Any])
async def get_job_history(job_id: str, limit: int = 10):
    """Get job execution history."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'jobs') or job_id not in gateway.jobs:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        job = gateway.jobs[job_id]
        
        history = job.get("history", [])[-limit:]
        
        return {
            "job_id": job_id,
            "history": history,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job history: {e}")
        raise HTTPException(status_code=500, detail=str(e))
