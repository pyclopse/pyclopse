"""Job scheduler for pyclaw."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from pyclaw.utils.time import now
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pyclaw.config.schema import JobsConfig
from .models import (
    AtSchedule, CronSchedule, IntervalSchedule,
    Job, JobRun, JobStatus,
    append_run_log, load_agent_jobs, load_jobs, read_run_log, save_agent_jobs, save_jobs,
)


class JobScheduler:
    """Async job scheduler supporting cron, interval, and one-shot jobs."""

    def __init__(
        self,
        config: JobsConfig,
        agent_executor: Optional[Callable] = None,
        notify_callback: Optional[Callable] = None,
        default_timezone: Optional[str] = None,
    ):
        self.config = config
        self.jobs: Dict[str, Job] = {}
        self._running_jobs: Set[str] = set()
        self._running_tasks: Dict[str, asyncio.Task] = {}   # job_id → asyncio task
        self._agent_executor = agent_executor      # async (job: Job) -> dict
        self._notify_callback = notify_callback    # async (job: Job, run: JobRun) -> None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclaw.jobs")
        self._agents_dir = Path(os.path.expanduser(config.agents_dir))
        # Tracks which agent each job belongs to: {job_id: agent_id}
        self._job_agents: Dict[str, str] = {}
        # Subagent tracking — all ephemeral, never persisted
        self._subagent_sessions: Dict[str, str] = {}          # job_id → session_id
        self._subagent_message_queue: Dict[str, List[str]] = {}  # job_id → pending msgs
        # Timezone used when a CronSchedule has no explicit timezone set.
        # Falls back to the system local timezone if neither config nor job specifies one.
        cfg_tz = getattr(config, "default_timezone", None)
        self._default_tz: str = default_timezone or cfg_tz or self._local_timezone()
        # Optional reference to the FileWatcher — used to acknowledge our own writes
        # so the watcher doesn't trigger a spurious reload after each _flush().
        self._file_watcher: Optional[Any] = None

    @staticmethod
    def _local_timezone() -> str:
        """Return the IANA name of the system local timezone."""
        # Preferred: read the symlink target of /etc/localtime (macOS + Linux)
        try:
            import os as _os
            link = _os.path.realpath("/etc/localtime")
            marker = "/zoneinfo/"
            idx = link.find(marker)
            if idx != -1:
                return link[idx + len(marker):]
        except Exception:
            pass
        # Fallback: Python's own local timezone name via datetime
        try:
            import datetime as _dt
            tz = _dt.datetime.now(_dt.timezone.utc).astimezone().tzinfo
            name = str(tz)
            if name and name != "UTC":
                return name
        except Exception:
            pass
        return "UTC"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.config.enabled:
            self._logger.info("Job scheduler disabled")
            return
        self._load_all_agent_jobs()
        for job in self.jobs.values():
            if job.enabled:
                # Always recalculate next_run on startup for cron and interval jobs.
                # Persisted next_run values may have been computed with wrong timezone
                # logic; recalculating from now() ensures the correct next fire time.
                # AtSchedule jobs keep their persisted time (they are absolute one-shots).
                from .models import AtSchedule
                if not isinstance(job.schedule, AtSchedule):
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
        self._flush()
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
        _now = now()
        for job in list(self.jobs.values()):
            if not job.enabled or job.id in self._running_jobs:
                continue
            if job.next_run and job.next_run <= _now:
                task = asyncio.create_task(self._run_job(job))
                self._running_tasks[job.id] = task

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
            started_at=now(),
            status=JobStatus.RUNNING,
        )
        job.status = JobStatus.RUNNING
        job.last_run = run.started_at
        job.run_count += 1
        job.updated_at = now()
        self._flush()

        try:
            self._logger.info(f"Running job: {job.name} [{job.run.kind}]")
            if self._notify_callback:
                asyncio.create_task(self._notify_callback(job, run))
            result = await self._execute(job)

            run.ended_at = now()
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
                # One-shot: delete after success
                if job.delete_after_run and isinstance(job.schedule, AtSchedule):
                    runs_dir = self._agent_runs_dir(job.id)
                    agent_id = self._job_agents.pop(job.id, None)
                    self.jobs.pop(job.id, None)
                    if agent_id:
                        agent_dir = self._agents_dir / agent_id
                        try:
                            disk_jobs = load_agent_jobs(agent_dir)
                            disk_jobs.pop(job.id, None)
                            save_agent_jobs(disk_jobs, agent_dir)
                        except Exception:
                            pass
                    append_run_log(run, runs_dir)
                    if self._notify_callback:
                        asyncio.create_task(self._notify_callback(job, run))
                    return
            else:
                job.status = JobStatus.FAILED
                job.failure_count += 1
                job.consecutive_errors += 1

            self._recalc_next_run(job)
            job.updated_at = now()
            self._flush()
            append_run_log(run, self._agent_runs_dir(job.id))

            self._logger.info(
                f"Job {job.name} {run.status.value} "
                f"({run.duration_ms():.0f}ms)"
            )

        except Exception as e:
            self._logger.error(f"Job {job.name} exception: {e}")
            run.ended_at = now()
            run.error = str(e)
            run.status = JobStatus.FAILED
            job.status = JobStatus.FAILED
            job.failure_count += 1
            job.consecutive_errors += 1
            self._recalc_next_run(job)
            job.updated_at = now()
            self._flush()
            append_run_log(run, self._agent_runs_dir(job.id))

        finally:
            self._running_jobs.discard(job.id)
            self._running_tasks.pop(job.id, None)
            # Clean up subagent tracking entries once the job is done
            if job.spawned_by_session is not None:
                self._subagent_sessions.pop(job.id, None)
                self._subagent_message_queue.pop(job.id, None)
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
            clean_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
            proc = await asyncio.create_subprocess_shell(
                job.run.command,
                env=clean_env,
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
        _now = now()
        s = job.schedule
        if isinstance(s, CronSchedule):
            tz = s.timezone or self._default_tz
            if self._is_continuous(s.expr):
                job.next_run = self._continuous_next(s.expr, _now, tz, s.stagger_seconds)
            else:
                job.next_run = self._cron_next(s.expr, _now, tz, s.stagger_seconds)
        elif isinstance(s, IntervalSchedule):
            job.next_run = _now + timedelta(seconds=s.seconds)
        elif isinstance(s, AtSchedule):
            job.next_run = s.at if s.at > _now else None
        else:
            job.next_run = None

    def _cron_next(
        self, expr: str, after: datetime, timezone: str = "UTC", stagger: int = 0
    ) -> Optional[datetime]:
        """Return the next run time as a naive datetime in the scheduler's local timezone.

        ``after`` is a naive datetime in the scheduler's configured timezone (same as
        ``now()``).  The cron expression is evaluated in ``timezone`` (the job's own
        timezone, defaulting to the scheduler's default).  The result is converted back
        to the scheduler's timezone so that it can be compared directly with ``now()``.
        """
        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo
            if not croniter.is_valid(expr):
                self._logger.warning(f"Invalid cron expression: {expr!r}")
                return after + timedelta(minutes=5)
            try:
                cron_tz = ZoneInfo(timezone)
                # ``after`` is naive in the scheduler's local timezone — attach the
                # correct tzinfo so the astimezone() conversion is accurate.
                pyclaw_tz = ZoneInfo(self._default_tz)
                after_aware = after.replace(tzinfo=pyclaw_tz)
                after_cron = after_aware.astimezone(cron_tz)
                next_cron = croniter(expr, after_cron).get_next(datetime)
                # Convert result back to scheduler tz and strip tzinfo for naive storage
                next_local = next_cron.astimezone(pyclaw_tz).replace(tzinfo=None)
            except Exception:
                # Fallback: evaluate without tz conversion (best-effort)
                next_local = croniter(expr, after).get_next(datetime)
            if stagger > 0:
                import random
                next_local += timedelta(seconds=random.randint(0, stagger))
            return next_local
        except Exception as e:
            self._logger.warning(f"Cron parse error {expr!r}: {e}")
            return after + timedelta(minutes=5)

    @staticmethod
    def _is_continuous(expr: str) -> bool:
        """Return True if the cron expression uses the 'continuous' minutes token."""
        return expr.strip().split()[0].lower() == "continuous"

    def _continuous_next(
        self, expr: str, after: datetime, timezone: str = "UTC", stagger: int = 0
    ) -> Optional[datetime]:
        """Return next run time for a continuous cron job.

        'continuous 7-14 * * 1-5' means: while inside the window defined by the
        remaining 4 cron fields, restart immediately after each run completes.
        When outside the window, schedule for the next window open.

        ``after`` is a naive datetime in the scheduler's local timezone.
        """
        parts = expr.strip().split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else "* * * * *"
        window_expr = f"* {rest}"
        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo

            pyclaw_tz = ZoneInfo(self._default_tz)
            try:
                cron_tz = ZoneInfo(timezone)
                after_aware = after.replace(tzinfo=pyclaw_tz)
                after_cron = after_aware.astimezone(cron_tz)
            except Exception:
                cron_tz = pyclaw_tz
                after_cron = after

            if croniter.match(window_expr, after_cron):
                # Still inside the window — restart immediately (plus optional stagger)
                next_local = after
                if stagger > 0:
                    import random
                    next_local = after + timedelta(seconds=random.randint(0, stagger))
                return next_local

            # Outside the window — find the next window open time
            next_cron = croniter(window_expr, after_cron).get_next(datetime)
            try:
                next_local = next_cron.astimezone(pyclaw_tz).replace(tzinfo=None)
            except Exception:
                next_local = next_cron.replace(tzinfo=None)
            if stagger > 0:
                import random
                next_local += timedelta(seconds=random.randint(0, stagger))
            return next_local

        except Exception as e:
            self._logger.warning(f"Continuous cron error {expr!r}: {e}")
            return after + timedelta(minutes=5)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_all_agent_jobs(self) -> None:
        """Discover and load all agents/*/jobs.yaml files."""
        if not self._agents_dir.exists():
            return
        for yaml_path in sorted(self._agents_dir.glob("*/jobs.yaml")):
            agent_id = yaml_path.parent.name
            try:
                agent_jobs = load_agent_jobs(yaml_path.parent)
                for job_id, job in agent_jobs.items():
                    self.jobs[job_id] = job
                    self._job_agents[job_id] = agent_id
                if agent_jobs:
                    self._logger.debug(f"Loaded {len(agent_jobs)} jobs for agent '{agent_id}'")
            except Exception as e:
                self._logger.error(f"Error loading jobs for agent '{agent_id}': {e}")

    def _agent_runs_dir(self, job_id: str) -> Path:
        """Return the runs directory for a job, scoped to its agent."""
        agent_id = self._job_agents.get(job_id)
        if agent_id:
            return self._agents_dir / agent_id / "runs"
        # Fallback: global runs dir
        return self._agents_dir.parent / "runs"

    # User-editable config fields — disk version wins on merge.
    # Runtime state fields — memory version always wins.
    _CONFIG_FIELDS = (
        "name", "schedule", "enabled", "run", "deliver",
        "timeout_seconds", "delete_after_run", "persistent",
    )

    def _merge_job_from_disk(self, mem_job: Job, disk_job: Job) -> Job:
        """Return a new Job with disk config fields + memory runtime fields.

        This preserves user edits (schedule, enabled, run definition, etc.)
        while keeping live scheduler state (last_run, run_count, status, etc.).
        """
        update = {
            field: getattr(disk_job, field)
            for field in self._CONFIG_FIELDS
            if hasattr(disk_job, field)
        }
        merged = mem_job.model_copy(update=update, deep=True)
        # Recalculate next_run if the schedule changed and the job isn't running
        if disk_job.schedule != mem_job.schedule and mem_job.id not in self._running_jobs:
            self._recalc_next_run(merged)
        return merged

    def _flush(self) -> None:
        """Write all in-memory jobs back to their per-agent YAML files.

        Groups jobs by agent, reads the current on-disk file, and performs a
        *smart merge*: for each job present in both memory and disk the config
        fields (schedule, enabled, run, …) come from disk (preserving manual
        edits) while the runtime fields (last_run, run_count, status, …) come
        from memory.  Jobs only in memory (newly added via API) are written as-is.
        Non-persistent jobs are never written to disk.
        """
        by_agent: Dict[str, Dict[str, Job]] = {}
        for job_id, job in self.jobs.items():
            if not job.persistent:
                continue
            agent_id = self._job_agents.get(job_id)
            if agent_id is None and job.run.kind == "agent":
                agent_id = job.run.agent
                self._job_agents[job_id] = agent_id
            if agent_id is None:
                self._logger.warning(f"Job {job.name!r} has no agent owner — skipping flush")
                continue
            by_agent.setdefault(agent_id, {})[job_id] = job

        for agent_id, mem_jobs in by_agent.items():
            agent_dir = self._agents_dir / agent_id
            jobs_path = agent_dir / "jobs.yaml"
            try:
                disk_jobs = load_agent_jobs(agent_dir)
            except Exception:
                disk_jobs = {}

            # Smart merge: start from disk (preserves manual edits), then
            # overlay memory's runtime state for each shared job.
            merged: Dict[str, Job] = dict(disk_jobs)
            for job_id, mem_job in mem_jobs.items():
                if job_id in disk_jobs:
                    merged[job_id] = self._merge_job_from_disk(mem_job, disk_jobs[job_id])
                else:
                    # Job exists only in memory (added via API / command)
                    merged[job_id] = mem_job

            try:
                save_agent_jobs(merged, agent_dir)
                # Tell the file watcher this write was ours so it doesn't
                # trigger a spurious reload on the next poll.
                if self._file_watcher is not None:
                    self._file_watcher.acknowledge(jobs_path)
            except Exception as e:
                self._logger.error(f"Error saving jobs for agent '{agent_id}': {e}")

    async def reload_agent_jobs(self, agent_id: str) -> None:
        """Reload jobs for *agent_id* from disk into memory.

        Called by the FileWatcher when jobs.yaml changes externally.  Applies
        disk config fields to existing in-memory jobs (preserving runtime state)
        and adds/disables jobs that were added/removed from the file.
        """
        agent_dir = self._agents_dir / agent_id
        try:
            disk_jobs = load_agent_jobs(agent_dir)
        except Exception as e:
            self._logger.warning(f"reload_agent_jobs: failed to load {agent_id}: {e}")
            return

        changes: List[str] = []

        for job_id, disk_job in disk_jobs.items():
            if job_id in self.jobs:
                mem_job = self.jobs[job_id]
                sched_changed = disk_job.schedule != mem_job.schedule
                enab_changed = disk_job.enabled != mem_job.enabled
                self.jobs[job_id] = self._merge_job_from_disk(mem_job, disk_job)
                if sched_changed:
                    changes.append(f"{disk_job.name}: schedule updated")
                if enab_changed:
                    changes.append(f"{disk_job.name}: enabled={disk_job.enabled}")
            else:
                # New job added to disk
                self.jobs[job_id] = disk_job
                self._job_agents[job_id] = agent_id
                if disk_job.enabled:
                    self._recalc_next_run(disk_job)
                changes.append(f"+{disk_job.name} (new)")

        # Disable jobs removed from disk (don't delete — could be accidental)
        agent_job_ids = {jid for jid, aid in self._job_agents.items() if aid == agent_id}
        for jid in agent_job_ids - set(disk_jobs):
            job = self.jobs.get(jid)
            if job and job.persistent and job.enabled:
                job.enabled = False
                job.next_run = None
                changes.append(f"-{job.name} (removed from disk, disabled)")

        if changes:
            self._logger.info(f"Reloaded jobs for '{agent_id}': {', '.join(changes)}")
        else:
            self._logger.debug(f"Reloaded jobs for '{agent_id}' (no changes detected)")

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

    async def add_job(self, job: Job, agent_id: Optional[str] = None) -> None:
        # Derive agent ownership
        if agent_id is None and job.run.kind == "agent":
            agent_id = job.run.agent
        if agent_id:
            self._job_agents[job.id] = agent_id
        if job.next_run is None:
            self._recalc_next_run(job)
        self.jobs[job.id] = job
        self._flush()
        self._logger.info(f"Added job: {job.name}")

    async def update_job(self, job: Job) -> None:
        self.jobs[job.id] = job
        self._recalc_next_run(job)
        job.updated_at = now()
        self._flush()

    async def remove_job(self, job_id: str) -> Optional[Job]:
        job = self.jobs.pop(job_id, None)
        agent_id = self._job_agents.pop(job_id, None)
        if job:
            if agent_id:
                agent_dir = self._agents_dir / agent_id
                try:
                    disk_jobs = load_agent_jobs(agent_dir)
                    # Merge remaining in-memory jobs for this agent with disk, drop removed
                    mem_for_agent = {jid: j for jid, j in self.jobs.items()
                                     if self._job_agents.get(jid) == agent_id}
                    merged = {**disk_jobs, **mem_for_agent}
                    merged.pop(job_id, None)
                    save_agent_jobs(merged, agent_dir)
                except Exception as e:
                    self._logger.error(f"Error removing job from disk: {e}")
            self._logger.info(f"Removed job: {job.name}")
        return job

    async def enable_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.enabled = True
        job.status = JobStatus.PENDING
        self._recalc_next_run(job)
        job.updated_at = now()
        self._flush()
        return True

    async def disable_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.enabled = False
        job.next_run = None
        job.status = JobStatus.DISABLED
        job.updated_at = now()
        self._flush()
        return True

    async def run_job_now(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        task = asyncio.create_task(self._run_job(job))
        self._running_tasks[job_id] = task
        return True

    async def list_jobs(self, owner: Optional[str] = None) -> List[Job]:
        jobs = list(self.jobs.values())
        if owner is not None:
            jobs = [j for j in jobs if self._job_agents.get(j.id) == owner]
        return jobs

    def get_run_history(self, job_id: str, limit: int = 20) -> List[JobRun]:
        return read_run_log(job_id, self._agent_runs_dir(job_id), limit)

    # ------------------------------------------------------------------
    # Subagent API
    # ------------------------------------------------------------------

    async def spawn_subagent(
        self,
        task: str,
        agent: str,
        spawned_by_session: str,
        model: Optional[str] = None,
        timeout_seconds: int = 300,
        prompt_preset: str = "minimal",
        instruction: Optional[str] = None,
    ) -> str:
        """Create and immediately fire a one-shot subagent job. Returns job_id."""
        from pyclaw.jobs.models import AgentRun, AtSchedule, DeliverNone, Job

        job_id = str(uuid.uuid4())
        job_name = f"subagent-{job_id[:8]}"

        agent_run = AgentRun(
            agent=agent,
            message=task,
            model=model,
            session_mode="isolated",
            prompt_preset=prompt_preset,
            instruction=instruction,
            report_to_session=spawned_by_session,
        )

        job = Job(
            id=job_id,
            name=job_name,
            persistent=False,
            spawned_by_session=spawned_by_session,
            run=agent_run,
            schedule=AtSchedule(at=now()),
            deliver=DeliverNone(),
            delete_after_run=True,
            timeout_seconds=timeout_seconds,
        )

        await self.add_job(job, agent_id=agent)
        await self.run_job_now(job_id)
        self._logger.info(f"Spawned subagent {job_name} for session {spawned_by_session[:8]}…")
        return job_id

    async def kill_subagent(self, job_id: str) -> bool:
        """Cancel a running subagent task. Returns True if it was found and cancelled."""
        task = self._running_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            self._running_jobs.discard(job_id)
            self._running_tasks.pop(job_id, None)
            self._subagent_sessions.pop(job_id, None)
            self._subagent_message_queue.pop(job_id, None)
            self.jobs.pop(job_id, None)
            self._job_agents.pop(job_id, None)
            self._logger.info(f"Killed subagent {job_id[:8]}…")
            return True
        return False

    def queue_message(self, job_id: str, message: str) -> bool:
        """Queue a follow-up message for a running subagent. Returns False if not found."""
        if job_id not in self.jobs:
            return False
        self._subagent_message_queue.setdefault(job_id, []).append(message)
        return True

    def pop_queued_messages(self, job_id: str) -> List[str]:
        """Drain and return all queued follow-up messages for a subagent."""
        return self._subagent_message_queue.pop(job_id, [])

    def list_subagents(self, spawned_by_agent: Optional[str] = None) -> List[Tuple[Job, str]]:
        """Return (job, session_id) tuples for all current in-memory subagent jobs.

        If spawned_by_agent is given, filter to jobs owned by that agent.
        """
        results = []
        for job in self.jobs.values():
            if job.spawned_by_session is None:
                continue
            owner = self._job_agents.get(job.id)
            if spawned_by_agent and owner != spawned_by_agent:
                continue
            session_id = self._subagent_sessions.get(job.id, "")
            results.append((job, session_id))
        return results
