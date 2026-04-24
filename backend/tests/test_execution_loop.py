"""Tests for the execution loop.

Uses a scripted fake TaskRunner — not the real Coder (which doesn't exist yet). Each test
asserts a real-world scenario the loop must handle: happy path, rework, budget exceeded,
crash recovery, etc.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any, AsyncIterator

import pytest

from app.agents.base import StreamEvent
from app.orchestrator.execution_loop import ExecutionLoop
from app.orchestrator.task_runner import TaskContext, TaskOutcome, TaskOutcomeKind
from app.sandbox import ProcessSandboxExecutor
from app.state import ProjectStatus, ProjectStore


# ---- Test helpers ---------------------------------------------------------------------


def make_task(
    task_id: str,
    *,
    phase: str = "P1",
    status: str = "pending",
    deps: list[str] | None = None,
    iterations: int = 0,
    budget_tokens: int = 50_000,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "phase": phase,
        "title": f"Task {task_id}",
        "description": "x",
        "acceptance_criteria": ["works"],
        "dependencies": deps or [],
        "status": status,
        "assigned_to": "coder",
        "iterations": iterations,
        "budget_tokens": budget_tokens,
        "notes": [],
    }


def setup_project(tmp: str, tasks: list[dict[str, Any]]) -> ProjectStore:
    """Create a project ready for the execution loop: tasks written, status EXECUTING,
    current_phase set to P1, and phases populated so PHASE_REVIEW transition can mark
    the phase done cleanly."""
    from app.state.store import ProjectPhase

    store = ProjectStore(tmp)
    store.init(project_id="proj_test", name="Test")
    store.write_tasks(tasks)
    meta = store.read_meta()
    meta.status = ProjectStatus.EXECUTING
    meta.current_phase = "P1"
    meta.phases = [ProjectPhase(id="P1", title="First", status="active", approved_by_user=True)]
    store.write_meta(meta)
    return store


class ScriptedTaskRunner:
    """Plays a scripted list of outcomes, one per call to run(), in order."""

    def __init__(self, outcomes: list[TaskOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.contexts_received: list[TaskContext] = []

    async def run(self, ctx: TaskContext) -> AsyncIterator[StreamEvent]:
        self.contexts_received.append(ctx)
        if not self._outcomes:
            raise RuntimeError(
                "ScriptedTaskRunner ran out of outcomes — test script too short"
            )
        outcome = self._outcomes.pop(0)
        yield StreamEvent(kind="task_outcome", payload={"outcome": outcome})


def collect_events(loop_run) -> list[StreamEvent]:
    """Drain an async generator into a list."""

    async def _drain() -> list[StreamEvent]:
        out: list[StreamEvent] = []
        async for ev in loop_run:
            out.append(ev)
        return out

    return asyncio.run(_drain())


# ---- Happy path ------------------------------------------------------------------------


def test_loop_halts_on_needs_user_review_and_stamps_review_fields() -> None:
    """When the Coder signals needs_user_review, the execution loop must:
      - Halt the phase (transition to AWAITING_TASK_REVIEW)
      - Stamp the review metadata onto the task so the UI has everything it needs
      - NOT run the next task in the dependency chain
    This is the critical path for UI/visual tasks that can't be verified from bash.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [
                make_task("P1-T1"),
                make_task("P1-T2", deps=["P1-T1"]),
            ],
        )
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(
                    kind=TaskOutcomeKind.NEEDS_USER_REVIEW,
                    summary="Built the canvas and game loop",
                    review_checklist=[
                        "Open index.html in a browser",
                        "Confirm canvas is 960x540 and visible",
                        "Check browser console has no errors",
                    ],
                    review_run_command="python -m http.server 8000 and open http://localhost:8000",
                    review_files_to_check=["index.html", "main.js"],
                ),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.AWAITING_TASK_REVIEW, (
            f"Expected AWAITING_TASK_REVIEW, got {final_meta.status}"
        )

        # T1 was the one in review; T2 must not have been run
        assert len(runner.contexts_received) == 1
        assert runner.contexts_received[0].task["id"] == "P1-T1"

        # T1's review fields should be populated on the task itself
        tasks = store.read_tasks()
        t1 = next(t for t in tasks if t["id"] == "P1-T1")
        assert t1["status"] == "review"
        assert t1["review_summary"] == "Built the canvas and game loop"
        assert len(t1["review_checklist"]) == 3
        assert "canvas" in t1["review_checklist"][1].lower()
        assert "http.server" in t1["review_run_command"]
        assert t1["review_files_to_check"] == ["index.html", "main.js"]
        assert isinstance(t1["review_requested_at"], (int, float))

        # T2 should still be pending
        t2 = next(t for t in tasks if t["id"] == "P1-T2")
        assert t2["status"] == "pending"

        # A task_needs_user_review event should be emitted
        assert any(e.kind == "task_needs_user_review" for e in events)


def test_loop_stamps_summary_and_completed_at_on_approved_task() -> None:
    """Approved tasks must have summary + completed_at fields on the task object itself,
    so the CompletedTasks UI can render them without joining against decisions.log
    (which is capped and will lose older entries on long projects)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(
                    kind=TaskOutcomeKind.APPROVED,
                    summary="Built the thing. Tests pass.",
                )
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        tasks = store.read_tasks()
        task = tasks[0]
        assert task["status"] == "done"
        assert task.get("summary") == "Built the thing. Tests pass."
        assert isinstance(task.get("completed_at"), (int, float))
        assert task["completed_at"] > 0


def test_loop_completes_phase_when_all_tasks_approved() -> None:
    """Three tasks in P1, all approved in order. With a second phase pending, P1 should
    complete and the project should transition to PHASE_REVIEW (not COMPLETE)."""
    from app.state.store import ProjectPhase

    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [
                make_task("P1-T1"),
                make_task("P1-T2", deps=["P1-T1"]),
                make_task("P1-T3", deps=["P1-T2"]),
                # A P2 task that's not part of the run — its presence is what makes
                # P1 completion trigger PHASE_REVIEW instead of PROJECT_COMPLETE.
                make_task("P2-T1", phase="P2"),
            ],
        )
        meta = store.read_meta()
        meta.phases = [
            ProjectPhase(id="P1", title="First", status="active", approved_by_user=True),
            ProjectPhase(id="P2", title="Second", status="pending", approved_by_user=False),
        ]
        store.write_meta(meta)

        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, summary="T1 done"),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, summary="T2 done"),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, summary="T3 done"),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.PHASE_REVIEW
        # Phase P1 itself should be marked done in meta.phases
        p1 = next(p for p in final_meta.phases if p.id == "P1")
        assert p1.status == "done"
        assert final_meta.tasks_completed == 3

        # All three P1 tasks should be done; P2 still pending
        tasks = store.read_tasks()
        p1_tasks = [t for t in tasks if t["phase"] == "P1"]
        assert all(t["status"] == "done" for t in p1_tasks)

        # Runner was called in dep order and only for P1 tasks
        assert [c.task["id"] for c in runner.contexts_received] == ["P1-T1", "P1-T2", "P1-T3"]

        # A phase_complete event was emitted
        assert any(e.kind == "phase_complete" for e in events)


def test_loop_completes_project_when_last_phase_finishes() -> None:
    """When there are no further phases and all tasks are done, project → COMPLETE
    directly (no PHASE_REVIEW needed for a final phase with nothing after it)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner([TaskOutcome(kind=TaskOutcomeKind.APPROVED)])
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.COMPLETE
        assert any(e.kind == "project_complete" for e in events)


def test_loop_follows_dependency_order() -> None:
    """If T1 must run before T2, the loop respects that."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [
                # Write them out of order — dep order should still be respected
                make_task("P1-T2", deps=["P1-T1"]),
                make_task("P1-T1"),
            ],
        )
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(kind=TaskOutcomeKind.APPROVED),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        assert [c.task["id"] for c in runner.contexts_received] == ["P1-T1", "P1-T2"]


# ---- Rework ---------------------------------------------------------------------------


def test_loop_retries_on_needs_rework() -> None:
    """First attempt needs rework, second attempt approved → task done, notes appended.

    Also verifies the Coder actually SEES the prior rework notes on iteration 2 via the
    TaskContext it receives. The storage check alone isn't enough — it would still pass
    if tools captured a stale task snapshot and the Coder ran iteration 2 blind.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(
                    kind=TaskOutcomeKind.NEEDS_REWORK,
                    rework_notes="Handle the empty array case",
                ),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        # Storage-level check: note was appended, iterations bumped, task done.
        tasks = store.read_tasks()
        assert tasks[0]["status"] == "done"
        assert tasks[0]["iterations"] == 2
        assert any("Handle the empty array" in n for n in tasks[0]["notes"])

        # Runner-level check: on iteration 2, the TaskContext the Coder received
        # must contain the prior rework note so the Coder can actually course-correct.
        # Two contexts total (one per attempt); the second one is iteration 2.
        assert len(runner.contexts_received) == 2
        second_ctx = runner.contexts_received[1]
        second_task = second_ctx.task
        assert second_task["iterations"] == 2
        assert any("Handle the empty array" in n for n in second_task.get("notes", [])), (
            f"Coder's iteration-2 context is missing prior rework notes. Got: "
            f"{second_task.get('notes')}"
        )


def test_loop_escalates_after_too_many_reworks() -> None:
    """If rework keeps happening, eventually the scheduler escalates (iterations budget)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        # Tighten iteration budget for a fast test
        meta = store.read_meta()
        meta.max_task_iterations = 3
        store.write_meta(meta)

        sandbox = ProcessSandboxExecutor(tmp)
        # 5 reworks in a row — loop should bail after hitting iteration cap
        runner = ScriptedTaskRunner(
            [TaskOutcome(kind=TaskOutcomeKind.NEEDS_REWORK, rework_notes="nope") for _ in range(5)]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED
        # Task should be marked blocked
        assert store.read_tasks()[0]["status"] == "blocked"
        # An escalation event should be emitted
        assert any(e.kind == "task_escalated" for e in events)
        # Runner should have been called ≤ max_iterations times (not all 5)
        assert len(runner.contexts_received) <= 3


# ---- Block / failure ------------------------------------------------------------------


def test_loop_halts_on_task_blocked_outcome() -> None:
    """If the runner returns BLOCKED, project goes to BLOCKED and loop exits."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [make_task("P1-T1"), make_task("P1-T2")],
        )
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(
                    kind=TaskOutcomeKind.BLOCKED,
                    block_reason="Dependency library has a known bug, need architect guidance",
                ),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED
        # T2 never ran because T1 blocked
        assert len(runner.contexts_received) == 1
        # The block reason is preserved in decisions
        decisions = store.read_decisions()
        assert any(
            d.get("kind") == "task_blocked" and "known bug" in d.get("reason", "")
            for d in decisions
        )


def test_loop_treats_runner_with_no_outcome_as_failure() -> None:
    """If a buggy TaskRunner forgets to emit an outcome event, we must NOT silently mark
    the task done. Treat it as failure and block the project."""

    class SilentRunner:
        async def run(self, ctx):
            # Yields a single event but no outcome
            yield StreamEvent(kind="text_delta", payload={"text": "hi"})

    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        sandbox = ProcessSandboxExecutor(tmp)
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=SilentRunner())
        collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED


# ---- Budget enforcement ---------------------------------------------------------------


def test_loop_halts_when_project_budget_exceeded_before_starting_task() -> None:
    """If tokens_used already exceeds budget, don't start another task."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        meta = store.read_meta()
        meta.project_token_budget = 10_000
        meta.tokens_used = 10_000  # already at cap
        store.write_meta(meta)

        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner([TaskOutcome(kind=TaskOutcomeKind.APPROVED)])
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED
        # Runner never invoked because we caught it at the budget gate
        assert len(runner.contexts_received) == 0
        assert any(e.kind == "budget_exceeded" for e in events)


def test_loop_accumulates_token_usage_across_tasks() -> None:
    """Token counts from each task outcome must roll up into project totals."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1"), make_task("P1-T2", deps=["P1-T1"])])
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, tokens_input=1000, tokens_output=500),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, tokens_input=2000, tokens_output=800),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.tokens_used == 1000 + 500 + 2000 + 800


# ---- Pause / resume -------------------------------------------------------------------


def test_loop_respects_paused_status_and_exits() -> None:
    """If the user pauses, the loop stops on the next iteration without running a task."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        meta = store.read_meta()
        meta.status = ProjectStatus.PAUSED
        store.write_meta(meta)

        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner([TaskOutcome(kind=TaskOutcomeKind.APPROVED)])
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        # Loop exited without invoking the runner
        assert len(runner.contexts_received) == 0
        assert any(e.kind == "loop_paused" for e in events)


# ---- Crash recovery -------------------------------------------------------------------


def test_loop_resets_tasks_stuck_in_progress_from_crash() -> None:
    """If a task is in_progress when the loop starts (previous process crashed mid-task),
    the loop must reset it to pending so the scheduler can pick it up again."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [
                make_task("P1-T1", status="in_progress", iterations=1),
                make_task("P1-T2", deps=["P1-T1"]),
            ],
        )
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner(
            [
                TaskOutcome(kind=TaskOutcomeKind.APPROVED),
                TaskOutcome(kind=TaskOutcomeKind.APPROVED),
            ]
        )
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        # Both tasks should complete — T1 got picked up after reset
        tasks = store.read_tasks()
        assert all(t["status"] == "done" for t in tasks)
        # T1's iteration count should be bumped again: 1 (before crash) + 1 (this run) = 2
        t1 = next(t for t in tasks if t["id"] == "P1-T1")
        assert t1["iterations"] == 2
        # And there should be a note about the reset
        assert any("Reset from in_progress" in n for n in t1["notes"])


# ---- Deadlock detection ---------------------------------------------------------------


def test_loop_halts_on_deadlock() -> None:
    """If a task depends on something that doesn't exist, loop halts with BLOCKED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(
            tmp,
            [make_task("P1-T1", deps=["does-not-exist"])],
        )
        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner([])  # Should never be called
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        events = collect_events(loop.run())

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED
        assert len(runner.contexts_received) == 0
        assert any(e.kind == "deadlock" for e in events)


# ---- Dispatching → Executing transition ----------------------------------------------


def test_loop_transitions_dispatching_to_executing_on_first_iteration() -> None:
    """If the loop is invoked while project is still in DISPATCHING, it transitions to
    EXECUTING on the first iteration rather than refusing to run."""
    with tempfile.TemporaryDirectory() as tmp:
        store = setup_project(tmp, [make_task("P1-T1")])
        meta = store.read_meta()
        meta.status = ProjectStatus.DISPATCHING  # pretend dispatcher just finished
        store.write_meta(meta)

        sandbox = ProcessSandboxExecutor(tmp)
        runner = ScriptedTaskRunner([TaskOutcome(kind=TaskOutcomeKind.APPROVED)])
        loop = ExecutionLoop(store=store, sandbox=sandbox, runner=runner)
        collect_events(loop.run())

        # Loop transitioned DISPATCHING → EXECUTING and ran the task through to COMPLETE.
        # (One-phase project so PROJECT_COMPLETE is the terminal state, not PHASE_REVIEW.)
        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.COMPLETE
        assert len(runner.contexts_received) == 1
