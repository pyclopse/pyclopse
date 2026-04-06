"""Job scheduler for pyclopse."""

import asyncio
from pyclopse.reflect import reflect_system
import logging
import os
import uuid
from datetime import datetime, timedelta
from pyclopse.utils.time import now
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pyclopse.config.schema import JobsConfig
from .models import (
    AtSchedule, CronSchedule, IntervalSchedule,
    Job, JobRun, JobStatus,
    append_run_log, load_agent_jobs, load_jobs, read_run_log, save_agent_jobs, save_jobs,
)


@reflect_system("jobs")
class JobScheduler:
    """Async job scheduler supporting cron, interval, and one-shot jobs.

    Jobs are persisted as YAML files under the configured agents directory and
    loaded on startup. The scheduler runs a 10-second polling loop that fires
    any job whose next_run time has passed. Cron expressions are evaluated using
    croniter; interval jobs are re-scheduled from completion time.

    Supports two run types (``CommandRun``, ``AgentRun``) and three notification
    types (``DeliverNone``, ``DeliverAnnounce``, ``DeliverWebhook``).

    ## Result delivery

    Delivery of job/subagent *results* is separate from the notification system
    and is controlled by fields on ``AgentRun``, not by the ``deliver`` config:

    ``report_to_agent`` (scheduled jobs)
        Dynamic routing — resolves to the named agent's current active session
        at delivery time.  Use this for scheduled jobs where the user session
        may have rolled over since the job was configured.  The active-session
        pointer is maintained by ``_get_active_session()`` in the gateway and
        only ever reflects user-facing channels (telegram, slack, TUI) — job
        sessions never overwrite it, so resolution is always safe.

    ``report_to_session`` (subagents)
        Pinned routing — delivers to an exact session ID captured at spawn time.
        Used by the subagent system so results return to the precise interactive
        session that spawned the subagent, not merely "wherever the agent is
        currently active".  Set automatically by ``spawn_subagent()`` from the
        ``x-session-id`` MCP request header — never set manually.

    Both paths converge at ``Gateway._deliver_result()`` which applies token-based
    delivery logic identically regardless of origin.

    ## Delivery tokens

    The agent's response text may begin with a token that controls how the result
    reaches the user:

    - ``NO_REPLY``  — result is injected into the agent's history so future turns
      have context, but nothing is sent to the user's channel.  Used for background
      tasks (heartbeats, pulse checks) where the agent should be aware but silent.
    - ``SUMMARIZE`` — result content is injected into history and the agent LLM is
      asked to summarize and relay it to the user.  Use when the raw output needs
      context, formatting, or the agent's voice.
    - *(no token)*  — verbatim delivery: result is injected into history AND sent
      directly to the user's channel without an additional LLM round-trip.  Use
      when the raw output is already user-ready.

    ## Subagent orchestration

    The subagent API (``spawn_subagent``, ``kill_subagent``, etc.) creates
    ephemeral one-shot ``AtSchedule`` jobs from within an agent session.  For
    top-level subagents (spawned from a user-facing session) results are delivered
    asynchronously via ``_deliver_to_spawning_session()``, keeping the user's
    session non-blocking.

    For sub-subagents (spawned from within a job/subagent session), async delivery
    is skipped to avoid ``_run_lock`` contention.  Instead, the result is cached
    in ``_subagent_results`` and the parent subagent retrieves it synchronously
    via the ``subagent_await()`` MCP tool.

    ## Notification vs delivery

    The ``deliver`` field on ``Job`` (``DeliverAnnounce`` etc.) controls *status
    notifications* only — the "▶️ started" / "✅ finished" channel pings sent by
    ``_job_notify``.  These are operationally separate from result delivery and go
    directly to the channel without touching the agent's history.

    Attributes:
        config (JobsConfig): Scheduler configuration from the gateway config.
        jobs (Dict[str, Job]): In-memory cache of all loaded jobs, keyed by job ID.
    """

    def __init__(
        self,
        config: JobsConfig,
        agent_executor: Optional[Callable] = None,
        notify_callback: Optional[Callable] = None,
        default_timezone: Optional[str] = None,
    ):
        """Initialise the scheduler with configuration and optional callbacks.

        Args:
            config (JobsConfig): Scheduler configuration (enabled flag, agents_dir, etc.).
            agent_executor (Optional[Callable]): Async callable with signature
                ``async (job: Job) -> dict`` used to dispatch agent-type jobs.
                If None, agent jobs will fail with "No agent executor configured".
            notify_callback (Optional[Callable]): Async callable with signature
                ``async (job: Job, run: JobRun) -> None`` invoked at job start and
                completion for real-time delivery of results.
            default_timezone (Optional[str]): IANA timezone name used as the fallback
                for cron expressions that do not specify a timezone. Falls back to the
                value in config, then to the system local timezone.
        """
        self.config = config
        self.jobs: Dict[str, Job] = {}
        self._running_jobs: Set[str] = set()
        self._running_tasks: Dict[str, asyncio.Task] = {}   # job_id → asyncio task
        self._agent_executor = agent_executor      # async (job: Job) -> dict
        self._notify_callback = notify_callback    # async (job: Job, run: JobRun) -> None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger("pyclopse.jobs")
        self._agents_dir = Path(os.path.expanduser(config.agents_dir))
        # Tracks which agent each job belongs to: {job_id: agent_id}
        self._job_agents: Dict[str, str] = {}
        # Subagent tracking — all ephemeral, never persisted
        self._subagent_sessions: Dict[str, str] = {}          # job_id → session_id
        self._subagent_message_queue: Dict[str, List[str]] = {}  # job_id → pending msgs
        # Result cache for sub-subagent orchestration via subagent_await().
        # Populated in _run_job before the job is deleted; consumed by subagent_await.
        self._subagent_results: Dict[str, str] = {}           # job_id → stdout
        # Timezone used when a CronSchedule has no explicit timezone set.
        # Falls back to the system local timezone if neither config nor job specifies one.
        cfg_tz = getattr(config, "default_timezone", None)
        self._default_tz: str = default_timezone or cfg_tz or self._local_timezone()
        # Optional reference to the FileWatcher — used to acknowledge our own writes
        # so the watcher doesn't trigger a spurious reload after each _flush().
        self._file_watcher: Optional[Any] = None

    @staticmethod
    def _local_timezone() -> str:
        """Return the IANA name of the system local timezone.

        Tries to read the symlink target of ``/etc/localtime`` first (works on
        macOS and most Linux systems), then falls back to Python's own datetime
        timezone detection, and finally returns "UTC" if both methods fail.

        Returns:
            str: IANA timezone name (e.g., "America/New_York", "Europe/London",
                or "UTC" as the ultimate fallback).
        """
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
        """Start the scheduler: load persisted jobs and begin the polling loop.

        If the scheduler is disabled via config, logs a message and returns
        without starting the loop. For enabled jobs, next_run is always
        recalculated from now() on startup (except AtSchedule one-shots whose
        absolute time is preserved).
        """
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
        """Stop the scheduler gracefully and flush job state to disk.

        Signals the polling loop to exit, waits up to 5 seconds for it to
        finish, then flushes all in-memory job state back to their YAML files.
        """
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
        """Run the main scheduling loop, waking every 10 seconds to check jobs.

        Calls ``_tick()`` on each iteration and exits cleanly when the stop
        event is set. Tick errors are logged but do not terminate the loop.
        """
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
        """Check all enabled jobs and fire any whose next_run time has arrived.

        Skips jobs that are already running. Creates an asyncio Task for each
        due job and stores it in ``_running_tasks``.
        """
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
        """Execute a single job, record the run, and reschedule.

        Guards against concurrent execution via ``_running_jobs``. Updates job
        state (status, last_run, run_count) before execution, dispatches to
        ``_execute()``, then records the result in the JSONL run log and calls
        the notify_callback. One-shot AtSchedule jobs with delete_after_run=True
        are removed from the registry on successful completion.

        Args:
            job (Job): The job to execute.
        """
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

        _notified = False
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
                    # Cache result before deletion so subagent_await() can retrieve it
                    # for sub-subagent orchestration (parent polls this dict).
                    self._subagent_results[job.id] = run.stdout or ""
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
                    _notified = True
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
            if self._notify_callback and not _notified:
                asyncio.create_task(self._notify_callback(job, run))

    async def _execute(self, job: Job) -> Dict[str, Any]:
        """Dispatch job execution to the appropriate executor based on run kind.

        Args:
            job (Job): The job to execute; ``job.run.kind`` selects the executor.

        Returns:
            Dict[str, Any]: Result dict with keys: ``success`` (bool), and optionally
                ``stdout``, ``stderr``, ``exit_code``, and ``error``.
        """
        if job.run.kind == "command":
            return await self._run_command(job)
        elif job.run.kind == "agent":
            return await self._run_agent(job)
        return {"success": False, "error": f"Unknown run kind: {job.run.kind}"}

    async def _run_command(self, job: Job) -> Dict[str, Any]:
        """Execute a shell command and return its stdout, stderr, and exit code.

        Runs the command in a subprocess using the current environment (with
        VIRTUAL_ENV stripped). Enforces ``job.timeout_seconds`` and kills the
        process on timeout.

        Args:
            job (Job): The job whose ``run.command`` string is executed.

        Returns:
            Dict[str, Any]: Result dict containing:
                ``success`` (bool), ``stdout`` (str), ``stderr`` (str),
                ``exit_code`` (int), and ``error`` (str) on failure.
        """
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
        """Dispatch an agent job via the configured agent_executor callable.

        Passes the whole Job to ``_agent_executor`` and enforces
        ``job.timeout_seconds``. Returns a failure dict if no executor is
        configured or if the executor raises or times out.

        Args:
            job (Job): The job whose run details are forwarded to the executor.

        Returns:
            Dict[str, Any]: Result dict from the agent executor, or a failure
                dict with ``success=False`` and an ``error`` message.
        """
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
        """Recalculate and set ``job.next_run`` based on the current time and schedule.

        Sets ``next_run`` to None for disabled jobs or one-shot AtSchedule jobs
        whose fire time has already passed.

        Args:
            job (Job): The job whose ``next_run`` field is updated in place.
        """
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
            # Anchor to last_run + interval so the cadence doesn't drift with
            # execution time or restart timing.  If the computed next time is
            # already in the past (long downtime, first run) fire immediately.
            if job.last_run is not None:
                candidate = job.last_run + timedelta(seconds=s.seconds)
                job.next_run = candidate if candidate > _now else _now
            else:
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
                pyclopse_tz = ZoneInfo(self._default_tz)
                after_aware = after.replace(tzinfo=pyclopse_tz)
                after_cron = after_aware.astimezone(cron_tz)
                next_cron = croniter(expr, after_cron).get_next(datetime)
                # Convert result back to scheduler tz and strip tzinfo for naive storage
                next_local = next_cron.astimezone(pyclopse_tz).replace(tzinfo=None)
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
        """Return True if the cron expression uses the 'continuous' minutes token.

        A continuous expression looks like: ``continuous 7-14 * * 1-5`` where
        the first token is the literal word "continuous" rather than a minute
        field.

        Args:
            expr (str): Cron expression string to check.

        Returns:
            bool: True if the first token is "continuous" (case-insensitive).
        """
        return expr.strip().split()[0].lower() == "continuous"

    def _continuous_next(
        self, expr: str, after: datetime, timezone: str = "UTC", stagger: int = 0
    ) -> Optional[datetime]:
        """Return the next run time for a continuous cron job.

        A "continuous" expression like ``continuous 7-14 * * 1-5`` means:
        while inside the window defined by the remaining four cron fields,
        restart immediately after each run completes. When outside the window,
        schedule for the next window open time.

        ``after`` is a naive datetime in the scheduler's local timezone.

        Args:
            expr (str): Continuous cron expression with leading "continuous" token
                followed by four standard cron fields.
            after (datetime): Naive datetime in the scheduler's local timezone;
                the next run must be at or after this moment.
            timezone (str): IANA timezone name for evaluating the window expression.
                Defaults to "UTC".
            stagger (int): Maximum random jitter in seconds added to the result.
                Defaults to 0.

        Returns:
            Optional[datetime]: Naive datetime of the next scheduled run, or
                ``after + 5 minutes`` as a fallback on parse error.
        """
        parts = expr.strip().split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else "* * * * *"
        window_expr = f"* {rest}"
        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo

            pyclopse_tz = ZoneInfo(self._default_tz)
            try:
                cron_tz = ZoneInfo(timezone)
                after_aware = after.replace(tzinfo=pyclopse_tz)
                after_cron = after_aware.astimezone(cron_tz)
            except Exception:
                cron_tz = pyclopse_tz
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
                next_local = next_cron.astimezone(pyclopse_tz).replace(tzinfo=None)
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
        """Discover and load all ``agents/*/jobs.yaml`` files under the agents directory.

        Iterates sorted glob matches so that agent loading order is deterministic.
        Failed agent loads are logged and skipped rather than raising.
        """
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
        """Return the runs directory for a job, scoped to its owning agent.

        Falls back to a global ``runs/`` directory under the agents root when
        no agent mapping is found for the job.

        Args:
            job_id (str): Job ID to look up.

        Returns:
            Path: Path to the appropriate runs directory for storing JSONL logs.
        """
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
        """Return a new Job combining disk config fields with in-memory runtime fields.

        Copies the fields listed in ``_CONFIG_FIELDS`` from ``disk_job`` onto a
        deep copy of ``mem_job``, preserving user edits (schedule, enabled, run
        definition, etc.) while keeping live scheduler state (last_run, run_count,
        status, etc.) from memory. If the schedule changed, ``next_run`` is
        recalculated immediately (unless the job is currently running).

        Args:
            mem_job (Job): Current in-memory job instance with live runtime state.
            disk_job (Job): Job instance freshly loaded from disk with current config.

        Returns:
            Job: A new Job instance with disk config and memory runtime state merged.
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
        """Reload jobs for an agent from disk into the in-memory job registry.

        Called by the FileWatcher when ``jobs.yaml`` changes externally. Applies
        disk config fields to existing in-memory jobs (preserving runtime state)
        and adds or disables jobs that were added or removed from the file.
        Jobs removed from disk are disabled rather than deleted to prevent
        accidental data loss.

        Args:
            agent_id (str): ID of the agent whose jobs.yaml should be reloaded.
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
        """Return a summary of the scheduler's current state.

        Returns:
            Dict[str, Any]: Dictionary with keys:
                ``enabled`` (bool): Whether scheduling is enabled in config.
                ``total`` (int): Total number of registered jobs.
                ``enabled_count`` (int): Number of jobs with enabled=True.
                ``running`` (int): Number of jobs currently executing.
        """
        return {
            "enabled": self.config.enabled,
            "total": len(self.jobs),
            "enabled_count": sum(1 for j in self.jobs.values() if j.enabled),
            "running": len(self._running_jobs),
        }

    def resolve(self, name_or_id: str) -> Optional[Job]:
        """Find a job by its ID or by its name (case-insensitive).

        ID lookup is tried first (exact match), then a case-insensitive name
        scan of all loaded jobs.

        Args:
            name_or_id (str): Job UUID string or human-readable job name.

        Returns:
            Optional[Job]: The matching Job, or None if not found.
        """
        if name_or_id in self.jobs:
            return self.jobs[name_or_id]
        needle = name_or_id.lower()
        for job in self.jobs.values():
            if job.name.lower() == needle:
                return job
        return None

    async def add_job(self, job: Job, agent_id: Optional[str] = None) -> None:
        """Register a new job with the scheduler and persist it to disk.

        Derives agent ownership from ``agent_id`` or from ``job.run.agent``
        for agent-type jobs. Calculates ``next_run`` if it is not already set,
        then flushes the updated job registry to YAML.

        Args:
            job (Job): The Job instance to register.
            agent_id (Optional[str]): Owner agent ID. If None and the job is
                an agent-type run, the agent name from ``job.run.agent`` is used.
        """
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
        """Replace an existing job in memory, recalculate next_run, and flush to disk.

        Args:
            job (Job): Updated Job instance. Must have the same ID as the job
                being replaced.
        """
        self.jobs[job.id] = job
        self._recalc_next_run(job)
        job.updated_at = now()
        self._flush()

    async def remove_job(self, job_id: str) -> Optional[Job]:
        """Remove a job from the scheduler and from its agent's YAML file.

        If the job has an agent owner, loads the agent's current on-disk jobs,
        merges remaining in-memory jobs, removes the target job, and saves.

        Args:
            job_id (str): UUID of the job to remove.

        Returns:
            Optional[Job]: The removed Job, or None if the ID was not found.
        """
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
        """Enable a job and schedule its next run.

        Sets ``enabled=True``, status to PENDING, recalculates ``next_run``,
        and flushes to disk.

        Args:
            job_id (str): UUID of the job to enable.

        Returns:
            bool: True if the job was found and enabled; False otherwise.
        """
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
        """Disable a job so it no longer runs.

        Sets ``enabled=False``, clears ``next_run``, sets status to DISABLED,
        and flushes to disk.

        Args:
            job_id (str): UUID of the job to disable.

        Returns:
            bool: True if the job was found and disabled; False otherwise.
        """
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
        """Immediately trigger a job regardless of its scheduled next_run time.

        Creates an asyncio Task for the job without waiting for it to complete.

        Args:
            job_id (str): UUID of the job to run immediately.

        Returns:
            bool: True if the job was found and fired; False if the ID is unknown.
        """
        job = self.jobs.get(job_id)
        if not job:
            return False
        task = asyncio.create_task(self._run_job(job))
        self._running_tasks[job_id] = task
        return True

    async def list_jobs(self, owner: Optional[str] = None) -> List[Job]:
        """Return all registered jobs, optionally filtered by owner agent ID.

        Args:
            owner (Optional[str]): If provided, only jobs whose ``_job_agents``
                entry matches this agent ID are returned.

        Returns:
            List[Job]: List of matching Job instances.
        """
        jobs = list(self.jobs.values())
        if owner is not None:
            jobs = [j for j in jobs if self._job_agents.get(j.id) == owner]
        return jobs

    def get_run_history(self, job_id: str, limit: int = 20) -> List[JobRun]:
        """Return the most recent run records for a job.

        Delegates to ``read_run_log`` using the job's agent-scoped runs directory.

        Args:
            job_id (str): UUID of the job whose history is requested.
            limit (int): Maximum number of records to return. Defaults to 20.

        Returns:
            List[JobRun]: Most-recent run records in chronological order.
        """
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
        report_to_agent: Optional[str] = None,
    ) -> str:
        """Create and immediately fire a one-shot ephemeral subagent job.

        The job is non-persistent (never written to YAML), uses an isolated
        session, and is scheduled to run at ``now()``. It is removed from the
        registry after successful completion via ``delete_after_run=True``.

        Args:
            task (str): The message/task text to send to the subagent.
            agent (str): Name of the agent to dispatch the task to.
            spawned_by_session (str): Session ID of the calling agent session;
                used to route the result back to the originating conversation.
            model (Optional[str]): Model override for the subagent run.
                Defaults to the agent's configured model.
            timeout_seconds (int): Maximum seconds the subagent may run.
                Defaults to 300.
            prompt_preset (str): Prompt preset name controlling system-prompt
                composition. Defaults to "minimal".
            instruction (Optional[str]): Optional extra instruction appended to
                the system prompt for this run.

        Returns:
            str: The UUID of the newly created subagent job.
        """
        from pyclopse.jobs.models import AgentRun, AtSchedule, DeliverNone, Job

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
            report_to_agent=report_to_agent,
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
        """Cancel a running subagent task and clean up all associated state.

        Cancels the asyncio Task, removes the job from the running set, task
        map, subagent tracking dicts, job registry, and agent ownership map.

        Args:
            job_id (str): UUID of the subagent job to cancel.

        Returns:
            bool: True if the task was found (running or pending) and cancelled;
                False if the job ID is unknown or the task has already finished.
        """
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
        """Queue a follow-up message for a running subagent job.

        The subagent's executor is expected to drain the queue (via
        ``pop_queued_messages``) between turns and inject the messages into the
        conversation.

        Args:
            job_id (str): UUID of the subagent job to message.
            message (str): Message text to append to the job's message queue.

        Returns:
            bool: True if the job exists and the message was queued;
                False if the job ID is not found.
        """
        if job_id not in self.jobs:
            return False
        self._subagent_message_queue.setdefault(job_id, []).append(message)
        return True

    def pop_queued_messages(self, job_id: str) -> List[str]:
        """Drain and return all queued follow-up messages for a subagent job.

        Removes all pending messages from the queue atomically. Returns an
        empty list if no messages are queued or the job ID is unknown.

        Args:
            job_id (str): UUID of the subagent job whose queue should be drained.

        Returns:
            List[str]: All pending message strings in FIFO order.
        """
        return self._subagent_message_queue.pop(job_id, [])

    def pop_subagent_result(self, job_id: str) -> Optional[str]:
        """Consume and return the cached result for a completed subagent job.

        Returns the subagent's stdout string if available, or ``None`` if the
        job has not yet completed or the result was already consumed.  Removes
        the entry from the cache on retrieval.

        Used by ``subagent_await()`` in the MCP tool layer to allow a parent
        subagent (job session) to synchronously receive a sub-subagent's result
        without going through ``handle_message()`` (which would deadlock on
        ``_run_lock``).

        Args:
            job_id (str): The UUID of the completed subagent job.

        Returns:
            Optional[str]: The subagent's stdout, or ``None`` if not yet ready.
        """
        return self._subagent_results.pop(job_id, None)

    def list_subagents(self, spawned_by_agent: Optional[str] = None) -> List[Tuple[Job, str]]:
        """Return (job, session_id) tuples for all current in-memory subagent jobs.

        Subagent jobs are identified by having a non-None ``spawned_by_session``
        field. Only jobs currently in the in-memory registry are returned;
        completed subagents that have been removed are not included.

        Args:
            spawned_by_agent (Optional[str]): If provided, only subagents owned
                by this agent ID are returned.

        Returns:
            List[Tuple[Job, str]]: List of (Job, session_id) tuples. ``session_id``
                is an empty string if the subagent's session has not yet been
                recorded in ``_subagent_sessions``.
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
