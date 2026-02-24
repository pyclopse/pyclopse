"""Job scheduler for pyclaw."""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
import uuid

from pyclaw.config.schema import JobsConfig
from .models import Job, JobRun, JobStatus, JobTrigger


class JobScheduler:
    """Job scheduler with cron-like functionality."""
    
    def __init__(
        self,
        config: JobsConfig,
        job_executor: Optional[Callable] = None,
    ):
        self.config = config
        self.jobs: Dict[str, Job] = {}
        self.runs: List[JobRun] = []
        self._running_jobs: Set[str] = set()
        self._executor = job_executor or self._default_executor
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclaw.jobs")
        self._persist_path = Path(os.path.expanduser(config.persist_file))
    
    async def start(self) -> None:
        """Start the scheduler."""
        if not self.config.enabled:
            self._logger.info("Jobs scheduler is disabled")
            return
        
        # Load persisted jobs
        await self._load_jobs()
        
        # Calculate initial next_run times
        for job in self.jobs.values():
            if job.enabled and job.next_run is None:
                self._calculate_next_run(job)
        
        # Start scheduler loop
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self._logger.info(f"Job scheduler started with {len(self.jobs)} jobs")
    
    async def stop(self) -> None:
        """Stop the scheduler."""
        self._stop_event.set()
        if self._scheduler_task:
            await self._scheduler_task
        await self._save_jobs()
        self._logger.info("Job scheduler stopped")
    
    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while not self._stop_event.is_set():
            try:
                await self._check_and_run_jobs()
            except Exception as e:
                self._logger.error(f"Scheduler error: {e}")
            
            # Sleep for 10 seconds between checks
            await asyncio.sleep(10)
    
    async def _check_and_run_jobs(self) -> None:
        """Check for jobs that need to run and execute them."""
        now = datetime.utcnow()
        
        for job in self.jobs.values():
            if not job.enabled:
                continue
            
            if job.status == JobStatus.RUNNING:
                continue
            
            if job.id in self._running_jobs:
                continue
            
            if job.next_run and job.next_run <= now:
                asyncio.create_task(self._run_job(job))
    
    async def _run_job(self, job: Job) -> None:
        """Run a single job."""
        if job.id in self._running_jobs:
            return
        
        self._running_jobs.add(job.id)
        
        try:
            job.update_status(JobStatus.RUNNING)
            await self._save_jobs()
            
            run_id = str(uuid.uuid4())
            run = JobRun(
                id=run_id,
                job_id=job.id,
                started_at=datetime.utcnow(),
                status=JobStatus.RUNNING,
            )
            self.runs.append(run)
            
            self._logger.info(f"Running job: {job.name} ({job.id})")
            
            # Execute the job
            result = await self._executor(job)
            
            run.ended_at = datetime.utcnow()
            run.status = JobStatus.COMPLETED if result.get("success") else JobStatus.FAILED
            run.stdout = result.get("stdout", "")
            run.stderr = result.get("stderr", "")
            run.exit_code = result.get("exit_code", 0)
            run.error = result.get("error")
            
            job.last_result = {
                "run_id": run_id,
                "success": run.status == JobStatus.COMPLETED,
                "duration_ms": (
                    (run.ended_at - run.started_at).total_seconds() * 1000
                ),
            }
            
            job.update_status(
                JobStatus.COMPLETED if run.status == JobStatus.COMPLETED else JobStatus.FAILED,
                job.last_result,
            )
            
            # Calculate next run
            self._calculate_next_run(job)
            await self._save_jobs()
            
            self._logger.info(
                f"Job {job.name} {run.status.value} "
                f"(exit code: {run.exit_code})"
            )
            
        except Exception as e:
            self._logger.error(f"Job {job.name} failed: {e}")
            job.update_status(JobStatus.FAILED, {"error": str(e)})
            await self._save_jobs()
        
        finally:
            self._running_jobs.discard(job.id)
    
    async def _default_executor(self, job: Job) -> Dict[str, Any]:
        """Default job executor using subprocess."""
        import time
        start_time = time.time()
        
        try:
            process = await asyncio.create_subprocess_shell(
                job.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=job.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "success": False,
                    "error": f"Job timed out after {job.timeout}s",
                    "exit_code": -1,
                }
            
            return {
                "success": process.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode,
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "exit_code": -1,
            }
    
    def _calculate_next_run(self, job: Job) -> None:
        """Calculate next run time based on trigger."""
        now = datetime.utcnow()
        
        if job.trigger == JobTrigger.CRON and job.cron_expression:
            # Simple cron parsing (basic support)
            job.next_run = self._parse_cron_next(job.cron_expression, now)
        
        elif job.trigger == JobTrigger.INTERVAL and job.interval_seconds:
            job.next_run = now + timedelta(seconds=job.interval_seconds)
        
        elif job.trigger == JobTrigger.ONESHOT:
            # One-shot jobs don't repeat
            job.next_run = None
        
        elif job.trigger == JobTrigger.MANUAL:
            # Manual jobs don't auto-schedule
            job.next_run = None
        
        else:
            job.next_run = None
    
    def _parse_cron_next(self, expression: str, now: datetime) -> Optional[datetime]:
        """Parse basic cron expression and calculate next run."""
        # Basic cron: minute hour day month weekday
        # This is simplified - full cron is complex
        parts = expression.split()
        if len(parts) < 5:
            return None
        
        # For now, just add 1 minute for any cron expression
        # A full implementation would use a cron library
        return now + timedelta(minutes=1)
    
    # Public API
    
    async def add_job(self, job: Job) -> None:
        """Add a new job."""
        if job.id in self.jobs:
            raise ValueError(f"Job {job.id} already exists")
        
        if job.next_run is None:
            self._calculate_next_run(job)
        
        self.jobs[job.id] = job
        await self._save_jobs()
        self._logger.info(f"Added job: {job.name} ({job.id})")
    
    async def remove_job(self, job_id: str) -> Optional[Job]:
        """Remove a job."""
        job = self.jobs.pop(job_id, None)
        if job:
            await self._save_jobs()
            self._logger.info(f"Removed job: {job.name} ({job_id})")
        return job
    
    async def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        return self.jobs.get(job_id)
    
    async def list_jobs(self) -> List[Job]:
        """List all jobs."""
        return list(self.jobs.values())
    
    async def enable_job(self, job_id: str) -> bool:
        """Enable a job."""
        job = self.jobs.get(job_id)
        if job:
            job.enabled = True
            if job.next_run is None:
                self._calculate_next_run(job)
            await self._save_jobs()
            return True
        return False
    
    async def disable_job(self, job_id: str) -> bool:
        """Disable a job."""
        job = self.jobs.get(job_id)
        if job:
            job.enabled = False
            job.next_run = None
            await self._save_jobs()
            return True
        return False
    
    async def run_job_now(self, job_id: str) -> bool:
        """Trigger a job to run immediately."""
        job = self.jobs.get(job_id)
        if job and job.enabled:
            asyncio.create_task(self._run_job(job))
            return True
        return False
    
    async def get_job_runs(
        self,
        job_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[JobRun]:
        """Get job run history."""
        runs = self.runs
        if job_id:
            runs = [r for r in runs if r.job_id == job_id]
        return runs[-limit:]
    
    # Persistence
    
    async def _load_jobs(self) -> None:
        """Load jobs from file."""
        if not self._persist_path.exists():
            return
        
        try:
            with open(self._persist_path, "r") as f:
                data = json.load(f)
            
            jobs_data = data.get("jobs", [])
            for job_data in jobs_data:
                job = Job.from_dict(job_data)
                self.jobs[job.id] = job
            
            runs_data = data.get("runs", [])
            for run_data in runs_data:
                run = JobRun.from_dict(run_data)
                self.runs.append(run)
            
            self._logger.info(f"Loaded {len(self.jobs)} jobs from {self._persist_path}")
            
        except Exception as e:
            self._logger.error(f"Error loading jobs: {e}")
    
    async def _save_jobs(self) -> None:
        """Save jobs to file."""
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                "jobs": [job.to_dict() for job in self.jobs.values()],
                "runs": [run.to_dict() for run in self.runs[-100:]],  # Keep last 100 runs
            }
            
            # Write atomically
            temp_path = self._persist_path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)
            temp_path.replace(self._persist_path)
            
        except Exception as e:
            self._logger.error(f"Error saving jobs: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get scheduler status."""
        return {
            "enabled": self.config.enabled,
            "jobs_total": len(self.jobs),
            "jobs_enabled": len([j for j in self.jobs.values() if j.enabled]),
            "jobs_running": len(self._running_jobs),
            "jobs_pending": len([j for j in self.jobs.values() if j.status == JobStatus.PENDING]),
        }
