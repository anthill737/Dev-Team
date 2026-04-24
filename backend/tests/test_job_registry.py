"""Tests for the JobRegistry — background execution lifecycle.

These are the invariants the registry must hold:
  - ensure_running is idempotent per project_id (no double-starts)
  - Subscribers see events, disconnecting a subscriber doesn't kill the job
  - Replay buffer gives a new subscriber catch-up of recent events
  - Job finish signals subscribers with the sentinel
  - Shutdown cancels running jobs cleanly
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.base import StreamEvent
from app.orchestrator.job_registry import JobRegistry


# ---- helpers -------------------------------------------------------------------


async def _yielding_loop(events: list[StreamEvent], pause: float = 0.0):
    """Yield each event in order with an optional pause between them.

    Used as a loop_factory for testing. Pause lets us control timing without
    real agent work."""
    for ev in events:
        if pause > 0:
            await asyncio.sleep(pause)
        yield ev


# ---- tests ---------------------------------------------------------------------


def test_ensure_running_is_idempotent() -> None:
    """Calling ensure_running twice for the same project_id returns the same job.

    This is critical because multiple endpoints (approve, resume, resume_execution,
    user_review) all call ensure_running — without idempotency we'd double-start."""

    async def _test():
        reg = JobRegistry()
        events = [StreamEvent(kind="x", payload={})]
        # Use a pause so the first job doesn't finish before we check
        job1 = await reg.ensure_running(
            "p1", lambda: _yielding_loop(events, pause=0.1)
        )
        job2 = await reg.ensure_running(
            "p1", lambda: _yielding_loop(events, pause=0.1)
        )
        assert job1 is job2
        assert reg.is_running("p1")
        # Clean up
        if job1.task:
            await job1.task

    asyncio.run(_test())


def test_subscribe_sees_live_events() -> None:
    """Subscribing to a running job delivers new events as they're emitted."""

    async def _test():
        reg = JobRegistry()
        events = [
            StreamEvent(kind="one", payload={"n": 1}),
            StreamEvent(kind="two", payload={"n": 2}),
        ]
        await reg.ensure_running("p1", lambda: _yielding_loop(events, pause=0.05))
        sub = await reg.subscribe("p1")
        assert sub is not None
        _replay, queue = sub

        received: list = []
        while True:
            ev = await asyncio.wait_for(queue.get(), timeout=2.0)
            if ev is None:
                break
            received.append(ev)
        kinds = [e.kind for e in received]
        # Some events may appear in replay instead of queue depending on timing,
        # so check the union
        all_kinds = [e.kind for e in _replay] + kinds
        assert "one" in all_kinds
        assert "two" in all_kinds

    asyncio.run(_test())


def test_unsubscribe_does_not_stop_job() -> None:
    """Core feature: a viewer disconnecting must NOT cancel the job."""

    async def _test():
        reg = JobRegistry()
        events = [
            StreamEvent(kind=f"e{i}", payload={}) for i in range(5)
        ]
        job = await reg.ensure_running(
            "p1", lambda: _yielding_loop(events, pause=0.02)
        )

        # Subscribe and immediately unsubscribe
        sub = await reg.subscribe("p1")
        assert sub is not None
        _, queue = sub
        await reg.unsubscribe("p1", queue)

        # The job should still complete on its own
        await asyncio.wait_for(job.task, timeout=2.0)
        assert job.finished_at is not None
        assert job.last_error is None

    asyncio.run(_test())


def test_subscribe_after_finish_gets_replay_and_sentinel() -> None:
    """A late-joining viewer attaches after the job ends: they still see
    the buffered events and then the None sentinel so the WS closes cleanly."""

    async def _test():
        reg = JobRegistry()
        events = [StreamEvent(kind="k", payload={"i": i}) for i in range(3)]
        job = await reg.ensure_running("p1", lambda: _yielding_loop(events))
        await asyncio.wait_for(job.task, timeout=2.0)  # Let it finish
        assert not job.is_running

        # Now subscribe — should work because the finished job is retained
        sub = await reg.subscribe("p1")
        assert sub is not None
        replay, queue = sub
        assert len(replay) == 3
        assert [e.kind for e in replay] == ["k", "k", "k"]
        # Queue should immediately have the None sentinel
        end_signal = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert end_signal is None

    asyncio.run(_test())


def test_job_error_emits_error_event_and_finishes() -> None:
    """If the loop_factory raises, the job records the error and finishes
    (doesn't just vanish). The project status should already have been flipped
    by the execution loop itself; this just tests the registry's error surface."""

    async def _test():
        reg = JobRegistry()

        async def bad_loop():
            yield StreamEvent(kind="before_crash", payload={})
            raise RuntimeError("test-crash")
            yield StreamEvent(kind="never", payload={})  # unreachable

        job = await reg.ensure_running("p1", bad_loop)
        await asyncio.wait_for(job.task, timeout=2.0)
        assert job.finished_at is not None
        assert "test-crash" in (job.last_error or "")
        # Replay should have the crash event too
        kinds = [e.kind for e in job.events]
        assert "job_error" in kinds

    asyncio.run(_test())


def test_shutdown_cancels_running_jobs() -> None:
    """On backend shutdown, the registry must cancel tasks cleanly."""

    async def _test():
        reg = JobRegistry()

        async def long_loop():
            try:
                while True:
                    await asyncio.sleep(1.0)
                    yield StreamEvent(kind="tick", payload={})
            except asyncio.CancelledError:
                raise

        await reg.ensure_running("p1", long_loop)
        await reg.ensure_running("p2", long_loop)
        assert reg.is_running("p1")
        assert reg.is_running("p2")

        await reg.shutdown()
        # Give the event loop a chance to process cancellations
        await asyncio.sleep(0.05)
        assert not reg.is_running("p1")
        assert not reg.is_running("p2")

    asyncio.run(_test())


def test_is_running_reflects_state() -> None:
    """is_running distinguishes running from finished jobs."""

    async def _test():
        reg = JobRegistry()
        assert not reg.is_running("p1")

        events = [StreamEvent(kind="x", payload={})]
        job = await reg.ensure_running("p1", lambda: _yielding_loop(events))
        # Could be running still (briefly) or already done — either way
        # let it finish and re-check
        await asyncio.wait_for(job.task, timeout=2.0)
        assert not reg.is_running("p1")

    asyncio.run(_test())
