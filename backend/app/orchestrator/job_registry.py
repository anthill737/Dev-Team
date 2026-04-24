"""Background job registry for execution loops.

Solves the "projects must keep running when no UI is watching" problem. Instead
of the execution WebSocket driving the loop directly (which died when the tab
closed), execution now runs as a background asyncio task registered here. The
WebSocket becomes a viewer: it subscribes, gets a replay of recent events, and
streams new events until disconnect. Disconnecting a viewer does NOT stop the job.

Key invariants:
  - One running job per project at a time (enforced via ensure_running).
  - Events buffered in a bounded deque so reconnecting viewers can catch up.
    Missing very old events (beyond the buffer) is acceptable — project state
    is always re-readable from disk via the HTTP API.
  - Subscriber queues are bounded; if a slow viewer can't keep up, events drop
    for that viewer rather than stalling the whole job. The viewer sees a
    "buffer overflow" marker and can refresh to resync from HTTP state.
  - Jobs are cleaned up some time after they finish so viewers can see the
    terminal state; stale cleanup is opportunistic, not strict.

Failure modes and where they land:
  - Job raises → event "job_error" with the exception; project status already
    flipped to BLOCKED by the execution loop itself. Viewer sees error + user
    can intervene via normal BLOCKED flow (retry dispatcher, etc.).
  - Job cancelled (backend shutdown) → no event emitted; project status stays
    as whatever it was. On next backend start the orchestrator's
    _reset_abandoned_in_progress picks up stuck tasks.
  - Subscriber queue full → that subscriber gets a "lagged" event and its
    queue drains enough to make room, preserving the newest events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


# How many events we buffer per job so a viewer who connects mid-run can replay
# recent activity. Bigger = better catch-up, more memory.
_RECENT_EVENTS_CAP = 500

# How many events we let a slow viewer queue before dropping. Sized larger than
# the replay buffer so normal viewers never lose events; lagging is rare.
_SUBSCRIBER_QUEUE_MAX = 1000

# How long after a job finishes before we remove it from the registry. Long
# enough that a user who navigates back after a project completes can still see
# the final event stream; short enough that memory doesn't grow unbounded.
_JOB_RETENTION_SECONDS = 600  # 10 minutes


@dataclass
class _JobEvent:
    """One event in a job's stream. `kind` + `payload` mirrors StreamEvent.
    `seq` is monotonic so viewers can detect gaps."""
    seq: int
    kind: str
    payload: dict[str, Any]


@dataclass
class RunningJob:
    """State for one active (or recently-finished) background execution.

    `task` is the asyncio.Task running the loop. When it finishes (success,
    error, or cancellation), `finished_at` is set. The job stays in the registry
    for _JOB_RETENTION_SECONDS after that so late-joining viewers can read it.
    """
    project_id: str
    task: asyncio.Task | None = None
    events: deque[_JobEvent] = field(default_factory=lambda: deque(maxlen=_RECENT_EVENTS_CAP))
    subscribers: list[asyncio.Queue[_JobEvent | None]] = field(default_factory=list)
    _next_seq: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # If the job crashed, stash the exception so viewers can see what happened
    # without us re-raising into the registry.
    last_error: str | None = None

    def next_seq(self) -> int:
        n = self._next_seq
        self._next_seq += 1
        return n

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()


class JobRegistry:
    """Singleton process-wide registry of background jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, RunningJob] = {}
        self._lock = asyncio.Lock()

    # ---- public API --------------------------------------------------------

    async def ensure_running(
        self,
        project_id: str,
        loop_factory: Callable[[], AsyncIterator[Any]],
    ) -> RunningJob:
        """Start a background job for a project if not already running.

        loop_factory is a no-arg callable that returns an async iterator of
        StreamEvents. We wrap it in a task and let it run to completion; events
        get broadcast to whoever's subscribed.

        Idempotent: calling this on an already-running project returns the
        existing job without starting a new one. This is how approve_plan can
        safely re-trigger execution without worrying about double-starts.
        """
        async with self._lock:
            existing = self._jobs.get(project_id)
            if existing is not None and existing.is_running:
                return existing

            # Evict any stale finished job for this project so we reuse the slot
            if existing is not None and not existing.is_running:
                self._jobs.pop(project_id, None)

            job = RunningJob(project_id=project_id)
            job.task = asyncio.create_task(
                self._run_job(job, loop_factory),
                name=f"job-{project_id}",
            )
            self._jobs[project_id] = job
            logger.info("Started background job for project %s", project_id)
            return job

    def get(self, project_id: str) -> RunningJob | None:
        """Return a running-or-recent job, or None if no job exists for this project."""
        self._evict_stale()
        return self._jobs.get(project_id)

    def is_running(self, project_id: str) -> bool:
        job = self._jobs.get(project_id)
        return job is not None and job.is_running

    async def subscribe(
        self, project_id: str
    ) -> tuple[list[_JobEvent], asyncio.Queue[_JobEvent | None]] | None:
        """Attach a new viewer to a running job.

        Returns (replay_events, live_queue) where replay_events is a snapshot
        of buffered events the viewer missed, and live_queue is where new
        events will arrive. If no job exists for this project, returns None
        and the caller should just read HTTP state.

        The live_queue yields `None` as a sentinel when the job is finished and
        the viewer should close cleanly.
        """
        async with self._lock:
            job = self._jobs.get(project_id)
            if job is None:
                return None
            replay = list(job.events)
            q: asyncio.Queue[_JobEvent | None] = asyncio.Queue(
                maxsize=_SUBSCRIBER_QUEUE_MAX
            )
            job.subscribers.append(q)
            # If the job already finished, tell the new subscriber immediately
            # so its WS loop can drain replay + close cleanly.
            if job.finished_at is not None:
                q.put_nowait(None)
            return replay, q

    async def unsubscribe(
        self, project_id: str, queue: asyncio.Queue[_JobEvent | None]
    ) -> None:
        async with self._lock:
            job = self._jobs.get(project_id)
            if job is None:
                return
            try:
                job.subscribers.remove(queue)
            except ValueError:
                pass

    async def shutdown(self) -> None:
        """Cancel all running jobs. Called on backend shutdown.

        Jobs see CancelledError and exit; the execution loop's finally blocks
        update state on disk so on next backend start the orchestrator can
        recover."""
        async with self._lock:
            tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("JobRegistry shutdown: cancelled %d running jobs", len(tasks))

    # ---- internals ---------------------------------------------------------

    async def _run_job(
        self,
        job: RunningJob,
        loop_factory: Callable[[], AsyncIterator[Any]],
    ) -> None:
        """Drive the execution loop, broadcast events, mark finished at the end."""
        try:
            async for ev in loop_factory():
                job_ev = _JobEvent(
                    seq=job.next_seq(),
                    kind=ev.kind,
                    payload=ev.payload,
                )
                job.events.append(job_ev)
                self._broadcast(job, job_ev)
        except asyncio.CancelledError:
            # Graceful shutdown — let it propagate to actually cancel.
            logger.info("Job %s cancelled", job.project_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s crashed", job.project_id)
            job.last_error = f"{type(exc).__name__}: {exc}"
            error_ev = _JobEvent(
                seq=job.next_seq(),
                kind="job_error",
                payload={"message": job.last_error},
            )
            job.events.append(error_ev)
            self._broadcast(job, error_ev)
        finally:
            job.finished_at = time.time()
            # Signal all subscribers that the stream is over
            for q in job.subscribers:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    # Subscriber is lagging; they'll notice when their queue
                    # eventually drains past the sentinel.
                    pass
            logger.info("Job %s finished", job.project_id)

    def _broadcast(self, job: RunningJob, event: _JobEvent) -> None:
        """Fan out one event to all current subscribers. Slow subscribers get
        dropped events rather than blocking the job."""
        for q in job.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Viewer is too slow. Drop oldest to make room for newest so
                # the viewer sees current state even if they missed a few.
                # Queues shouldn't usually hit this cap in practice.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _evict_stale(self) -> None:
        """Remove finished jobs older than retention. Cheap, called on gets."""
        now = time.time()
        stale = [
            pid
            for pid, j in self._jobs.items()
            if j.finished_at is not None
            and (now - j.finished_at) > _JOB_RETENTION_SECONDS
        ]
        for pid in stale:
            self._jobs.pop(pid, None)


# Module-level singleton. The backend has one JobRegistry for the lifetime of
# the process. Imports are cheap (the registry constructs lazily on first use).
_registry: JobRegistry | None = None


def get_registry() -> JobRegistry:
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry
