"""Job scheduler for pyclaw."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from pyclaw.config.schema import JobsConfig
from .models import (
    AtSchedule, CronSchedule, IntervalSchedule,
    Job, JobRun, JobStatus,
    append_run_log, load_jobs, read_run_log, save_jobs,
)


class JobScheduler:
    """Async job scheduler supporting cron, interval, and one-shot jobs."""

    def __init__(
        self,
        config: JobsConfig,
        agent_executor: Optional[Callable] = None,
        notify_callback: Optional[Callable] = None,
    ):
        self.config = config
        self.jobs: Dict[str, Job] = {}
        self._running_jobs: Set[str] = set()
        self._agent_executor = agent_executor      # async (job: Job) -> dict
        self._notify_callback = notify_callback    # async (job: Job, run: JobRun) -> None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclaw.jobs")
        self._persist_path = Path(os.path.expanduser(config.persist_file))
        self._runs_dir = self._persist_path.parent / "runs"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.config.enabled:
            self._logger.info("Job scheduler disabled")
            return
        self.jobs = load_jobs(self._persist_path)
        for job in self.jobs.values():
            if job.enabled and job.next_run is None:
                self._recalc_next_run(job)
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(self._loop())
        self._logger.info(f"Job scheduler started with {len(self.jobs)} jobs")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler_task:
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=5.0)
            except asyncio.TimeoutError:
                pass
        save_jobs(self.jobs, self._persist_path)
        self._logger.info("Job scheduler stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                self._logger.error(f"Scheduler tick error: {e}")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()), timeout=10.0
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = datetime.utcnow()
        for job in list(self.jobs.values()):
            if not job.enabled or job.id in self._running_jobs:
                continue
            if job.next_run and job.next_run <= now:
                asyncio.create_task(self._run_job(job))

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _run_job(self, job: Job) -> None:
        if job.id in self._running_jobs:
            return
        self._running_jobs.add(job.id)
        run = JobRun(
            id=str(uuid.uuid4()),
            job_id=job.id,
            job_name=job.name,
            started_at=datetime.utcnow(),
            status=JobStatus.RUNNING,
        )
        job.status = JobStatus.RUNNING
        job.last_run = run.started_at
        job.run_count += 1
        job.updated_at = datetime.utcnow()
        save_jobs(self.jobs, self._persist_path)

        try:
            self._logger.info(f"Running job: {job.name} [{job.run.kind}]")
            result = await self._execute(job)

            run.ended_at = datetime.utcnow()
            run.stdout = result.get("stdout", "")
            run.stderr = result.get("stderr", "")
            run.exit_code = result.get("exit_code")
            run.error = result.get("error")
            run.status = JobStatus.COMPLETED if result.get("success") else JobStatus.FAILED

            job.last_result = {
                "run_id": run.id,
                "success": run.status == JobStatus.COMPLETED,
                "duration_ms": run.duration_ms(),
            }

            if run.status == JobStatus.COMPLETED:
                job.status = JobStatus.COMPLETED
                job.consecutive_errors = 0
                # One-shot: delete or disable after success
                if job.delete_after_run and isinstance(job.schedule, AtSchedule):
                    self.jobs.pop(job.id, None)
                    save_jobs(self.jobs, self._persist_path)
                    append_run_log(run, self._runs_dir)
                    if self._notify_callback:
                        asyncio.create_task(self._notify_callback(job, run))
                    return
            else:
                job.status = JobStatus.FAILED
                job.failure_count += 1
                job.consecutive_errors += 1

            self._recalc_next_run(job)
            job.updated_at = datetime.utcnow()
            save_jobs(self.jobs, self._persist_path)
            append_run_log(run, self._runs_dir)

            self._logger.info(
                f"Job {job.name} {run.status.value} "
                f"({run.duration_ms():.0f}ms)"
            )

        except Exception as e:
            self._logger.error(f"Job {job.name} exception: {e}")
            run.ended_at = datetime.utcnow()
            run.error = str(e)
            run.status = JobStatus.FAILED
            job.status = JobStatus.FAILED
            job.failure_count += 1
            job.consecutive_errors += 1
            self._recalc_next_run(job)
            job.updated_at = datetime.utcnow()
            save_jobs(self.jobs, self._persist_path)
            append_run_log(run, self._runs_dir)

        finally:
            self._running_jobs.discard(job.id)
            if self._notify_callback:
                asyncio.create_task(self._notify_callback(job, run))

    async def _execute(self, job: Job) -> Dict[str, Any]:
        """Dispatch to the right executor based on run kind."""
        if job.run.kind == "command":
            return await self._run_command(job)
        elif job.run.kind == "agent":
            return await self._run_agent(job)
        return {"success": False, "error": f"Unknown run kind: {job.run.kind}"}

    async def _run_command(self, job: Job) -> Dict[str, Any]:
        """Execute a shell command."""
        try:
            proc = await asyncio.create_subprocess_shell(
                job.run.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=job.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"success": False, "error": f"Timed out after {job.timeout_seconds}s", "exit_code": -1}

            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "exit_code": -1}

    async def _run_agent(self, job: Job) -> Dict[str, Any]:
        """Send a message to an agent and return its response as stdout."""
        if not self._agent_executor:
            return {"success": False, "error": "No agent executor configured"}
        try:
            result = await asyncio.wait_for(
                self._agent_executor(job),
                timeout=job.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Agent timed out after {job.timeout_seconds}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _recalc_next_run(self, job: Job) -> None:
        if not job.enabled:
            job.next_run = None
            return
        now = datetime.utcnow()
        s = job.schedule
        if isinstance(s, CronSchedule):
            job.next_run = self._cron_next(s.expr, now, s.timezone, s.stagger_seconds)
        elif isinstance(s, IntervalSchedule):
            job.next_run = now + timedelta(seconds=s.seconds)
        elif isinstance(s, AtSchedule):
            job.next_run = s.at if s.at > now else None
        else:
            job.next_run = None

    def _cron_next(
        self, expr: str, after: datetime, timezone: str = "UTC", stagger: int = 0
    ) -> Optional[datetime]:
        try:
            from croniter import croniter
            if not croniter.is_valid(expr):
                self._logger.warning(f"Invalid cron expression: {expr!r}")
                return after + timedelta(minutes=5)
            if timezone and timezone != "UTC":
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(timezone)
                    after_local = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
                    next_local = croniter(expr, after_local).get_next(datetime)
                    next_utc = next_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                except Exception:
                    next_utc = croniter(expr, after).get_next(datetime)
            else:
                next_utc = croniter(expr, after).get_next(datetime)
            if stagger > 0:
                import random
                next_utc += timedelta(seconds=random.randint(0, stagger))
            return next_utc
        except Exception as e:
            self._logger.warning(f"Cron parse error {expr!r}: {e}")
            return after + timedelta(minutes=5)

    # ------------------------------------------------------------------
    # Public API (called by HTTP routes)
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "total": len(self.jobs),
            "enabled_count": sum(1 for j in self.jobs.values() if j.enabled),
            "running": len(self._running_jobs),
        }

    def resolve(self, name_or_id: str) -> Optional[Job]:
        """Find a job by ID or by name (case-insensitive)."""
        if name_or_id in self.jobs:
            return self.jobs[name_or_id]
        needle = name_or_id.lower()
        for job in self.jobs.values():
            if job.name.lower() == needle:
                return job
        return None

    async def add_job(self, job: Job) -> None:
        if job.next_run is None:
            self._recalc_next_run(job)
        self.jobs[job.id] = job
        save_jobs(self.jobs, self._persist_path)
        self._logger.info(f"Added job: {job.name}")

    async def update_job(self, job: Job) -> None:
        self.jobs[job.id] = job
        self._recalc_next_run(job)
        job.updated_at = datetime.utcnow()
        save_jobs(self.jobs, self._persist_path)

    async def remove_job(self, job_id: str) -> Optional[Job]:
        job = self.jobs.pop(job_id, None)
        if job:
            save_jobs(self.jobs, self._persist_path)
            self._logger.info(f"Removed job: {job.name}")
        return job

    async def enable_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.enabled = True
        job.status = JobStatus.PENDING
        self._recalc_next_run(job)
        job.updated_at = datetime.utcnow()
        save_jobs(self.jobs, self._persist_path)
        return True

    async def disable_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.enabled = False
        job.next_run = None
        job.status = JobStatus.DISABLED
        job.updated_at = datetime.utcnow()
        save_jobs(self.jobs, self._persist_path)
        return True

    async def run_job_now(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        asyncio.create_task(self._run_job(job))
        return True

    async def list_jobs(self, owner: Optional[str] = None) -> List[Job]:
        jobs = list(self.jobs.values())
        if owner is not None:
            jobs = [j for j in jobs if j.owner == owner]
        return jobs

    def get_run_history(self, job_id: str, limit: int = 20) -> List[JobRun]:
        return read_run_log(job_id, self._runs_dir, limit)
