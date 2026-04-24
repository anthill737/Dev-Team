"""Execution loop: drive the project from DISPATCHING through EXECUTING to phase review.

This is the piece that sits between the scheduler (pure "what's next?") and the task
runner (the Coder, eventually). On each turn of the loop we:

  1. Check for user pause / cancellation.
  2. Ask the scheduler for the next action given current tasks.json + project phase.
  3. Dispatch on the scheduler's decision:
       - RUN_TASK       → mark task in_progress, invoke runner, update based on outcome
       - WAITING        → should not happen if we've been managing state; log and break
       - PHASE_COMPLETE → transition to PHASE_REVIEW, hand back to user
       - PROJECT_COMPLETE → transition to COMPLETE
       - ESCALATE_TASK  → transition project to BLOCKED, note reason, hand to user
       - DEADLOCK       → transition project to BLOCKED, note reason, hand to user
  4. After task completion, check budgets; if exceeded, escalate to BLOCKED.

The loop yields StreamEvents for live UI, same shape as the architect/dispatcher streams.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from ..agents.base import StreamEvent
from ..sandbox import SandboxExecutor
from ..state import ProjectStatus, ProjectStore
from .scheduler import SchedulerDecisionKind, choose_next_action
from .task_runner import TaskContext, TaskOutcome, TaskOutcomeKind, TaskRunner

logger = logging.getLogger(__name__)


# Safety caps on the loop itself, independent of per-task budgets. Prevents a bug in a
# task runner from looping forever even if nothing thinks the budget is exceeded.
_MAX_LOOP_ITERATIONS = 100


class ExecutionLoop:
    """Drives execution of an approved phase."""

    def __init__(
        self,
        store: ProjectStore,
        sandbox: SandboxExecutor,
        runner: TaskRunner,
        coder_model: str = "claude-sonnet-4-6",
    ) -> None:
        self.store = store
        self.sandbox = sandbox
        self.runner = runner
        self._coder_model = coder_model

    async def run(self) -> AsyncIterator[StreamEvent]:
        """Run the execution loop until a terminal state. Yields events for live UI."""
        # Reset any tasks stuck in_progress from a prior crash. If we died mid-task,
        # that task shouldn't stay in limbo — it goes back to pending so the scheduler
        # can pick it up again.
        self._reset_abandoned_in_progress()

        iterations = 0
        while iterations < _MAX_LOOP_ITERATIONS:
            iterations += 1

            # Respect user pause between tasks
            meta = self.store.read_meta()
            if meta.status == ProjectStatus.PAUSED:
                yield _event("loop_paused", reason="Project paused by user")
                return

            # Only proceed from EXECUTING or DISPATCHING states. Any other state means
            # something else (the user, the architect, etc.) has control.
            if meta.status not in (ProjectStatus.EXECUTING, ProjectStatus.DISPATCHING):
                yield _event(
                    "loop_exit",
                    reason=f"Project no longer in an executable state: {meta.status.value}",
                )
                return

            # Transition DISPATCHING → EXECUTING on first iteration after a dispatch
            if meta.status == ProjectStatus.DISPATCHING:
                meta.status = ProjectStatus.EXECUTING
                self.store.write_meta(meta)

            tasks = self.store.read_tasks()
            decision = choose_next_action(
                tasks,
                current_phase=meta.current_phase,
                max_iterations=meta.max_task_iterations,
            )

            yield _event(
                "scheduler_decision",
                decision_kind=decision.kind.value,
                task_id=decision.task["id"] if decision.task else None,
                reason=decision.reason,
            )

            if decision.kind == SchedulerDecisionKind.PROJECT_COMPLETE:
                meta.status = ProjectStatus.COMPLETE
                self.store.write_meta(meta)
                await self.store.append_decision(
                    {"actor": "orchestrator", "kind": "project_complete"}
                )
                yield _event("project_complete")
                return

            if decision.kind == SchedulerDecisionKind.PHASE_COMPLETE:
                meta.status = ProjectStatus.PHASE_REVIEW
                # Mark the current phase done in meta.phases
                for p in meta.phases:
                    if p.id == meta.current_phase:
                        p.status = "done"
                self.store.write_meta(meta)
                await self.store.append_decision(
                    {
                        "actor": "orchestrator",
                        "kind": "phase_complete",
                        "phase": meta.current_phase,
                    }
                )
                yield _event("phase_complete", phase=meta.current_phase)
                return

            if decision.kind == SchedulerDecisionKind.ESCALATE_TASK:
                task = decision.task
                assert task is not None  # scheduler guarantees task present for this kind
                self.store.update_task(task["id"], {"status": "blocked"})
                meta.status = ProjectStatus.BLOCKED
                self.store.write_meta(meta)
                await self.store.append_decision(
                    {
                        "actor": "orchestrator",
                        "kind": "task_escalated",
                        "task_id": task["id"],
                        "reason": decision.reason,
                    }
                )
                yield _event("task_escalated", task_id=task["id"], reason=decision.reason)
                return

            if decision.kind == SchedulerDecisionKind.DEADLOCK:
                meta.status = ProjectStatus.BLOCKED
                self.store.write_meta(meta)
                await self.store.append_decision(
                    {"actor": "orchestrator", "kind": "deadlock", "reason": decision.reason}
                )
                yield _event("deadlock", reason=decision.reason)
                return

            if decision.kind == SchedulerDecisionKind.WAITING:
                # Shouldn't happen in a clean loop — we always finish a task before asking
                # the scheduler again. But if it does (e.g., external state mutation), log
                # and exit rather than spinning.
                yield _event(
                    "loop_exit",
                    reason=f"Scheduler reports WAITING unexpectedly: {decision.reason}",
                )
                return

            if decision.kind == SchedulerDecisionKind.RUN_TASK:
                task = decision.task
                assert task is not None

                # Budget enforcement BEFORE running: if we can't afford another task, stop.
                remaining = meta.project_token_budget - meta.tokens_used
                task_budget = min(task.get("budget_tokens", 50_000), remaining)
                if task_budget <= 0:
                    meta.status = ProjectStatus.BLOCKED
                    self.store.write_meta(meta)
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "budget_exceeded",
                            "reason": (
                                f"Project token budget ({meta.project_token_budget}) reached; "
                                f"cannot start {task['id']}."
                            ),
                        }
                    )
                    yield _event(
                        "budget_exceeded",
                        task_id=task["id"],
                        budget=meta.project_token_budget,
                    )
                    return

                # Mark the task in_progress and bump its iteration count BEFORE running,
                # so if we crash during the run, the reset-on-startup logic sees
                # iterations+1 and can decide whether to retry or escalate.
                new_iterations = task.get("iterations", 0) + 1
                self.store.update_task(
                    task["id"], {"status": "in_progress", "iterations": new_iterations}
                )

                await self.store.append_decision(
                    {
                        "actor": "orchestrator",
                        "kind": "task_start",
                        "task_id": task["id"],
                        "iteration": new_iterations,
                    }
                )
                yield _event("task_start", task_id=task["id"], iteration=new_iterations)

                # Delegate to the TaskRunner. It yields its own events; we relay them.
                # NOTE (deferred): there's no wall-clock timeout on the runner here. If a
                # real Coder hangs indefinitely on a task, this loop will wait forever.
                # When the Coder lands, it should enforce its own time/token budgets and
                # return a FAILED outcome rather than hanging. If that proves unreliable,
                # add an asyncio.wait_for wrapper around the generator.
                ctx = TaskContext(
                    task={**task, "iterations": new_iterations},
                    store=self.store,
                    sandbox=self.sandbox,
                    project_token_budget_remaining=remaining,
                    task_token_budget=task_budget,
                )
                outcome: TaskOutcome | None = None
                async for ev in self.runner.run(ctx):
                    if ev.kind == "task_outcome":
                        outcome = ev.payload.get("outcome")
                    yield ev

                if outcome is None:
                    # Runner didn't emit an outcome — treat as failure and escalate.
                    outcome = TaskOutcome(
                        kind=TaskOutcomeKind.FAILED,
                        failure_reason="Task runner did not report an outcome",
                    )

                # Update tokens used regardless of outcome. Coder always uses the
                # configured Coder model (Sonnet by default).
                self.store.add_token_usage(
                    model=self._coder_model,
                    tokens_input=outcome.tokens_input,
                    tokens_output=outcome.tokens_output,
                    cache_read=outcome.cache_read_tokens,
                    cache_creation=outcome.cache_creation_tokens,
                )
                meta = self.store.read_meta()

                # Apply the outcome to task state
                if outcome.kind == TaskOutcomeKind.APPROVED:
                    # Stamp the summary and completion time directly onto the task so
                    # the UI can render "Completed tasks" from tasks.json without
                    # joining against the decisions log (which is capped by pagination
                    # and would lose older completions on long projects).
                    import time as _time

                    self.store.update_task(
                        task["id"],
                        {
                            "status": "done",
                            "summary": outcome.summary,
                            "completed_at": _time.time(),
                        },
                    )
                    meta.tasks_completed += 1
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "task_approved",
                            "task_id": task["id"],
                            "summary": outcome.summary,
                        }
                    )
                elif outcome.kind == TaskOutcomeKind.NEEDS_USER_REVIEW:
                    # Halt the phase. Stamp review metadata onto the task so the UI has
                    # everything it needs to prompt the user. When user approves or
                    # rejects via the review API, project transitions back to EXECUTING
                    # and the loop resumes (on next WS open).
                    import time as _time

                    self.store.update_task(
                        task["id"],
                        {
                            "status": "review",
                            "review_summary": outcome.summary,
                            "review_checklist": list(outcome.review_checklist),
                            "review_run_command": outcome.review_run_command,
                            "review_files_to_check": list(outcome.review_files_to_check),
                            "review_requested_at": _time.time(),
                        },
                    )
                    meta.status = ProjectStatus.AWAITING_TASK_REVIEW
                    self.store.write_meta(meta)
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "task_needs_user_review",
                            "task_id": task["id"],
                            "summary": outcome.summary,
                        }
                    )
                    yield _event(
                        "task_needs_user_review",
                        task_id=task["id"],
                        summary=outcome.summary,
                    )
                    return
                elif outcome.kind == TaskOutcomeKind.NEEDS_REWORK:
                    # Back to pending — scheduler will pick it up again, iteration counter
                    # already bumped. Rework notes go into task.notes for the next pass.
                    existing_notes = list(task.get("notes", []))
                    existing_notes.append(f"Rework: {outcome.rework_notes}")
                    self.store.update_task(
                        task["id"], {"status": "pending", "notes": existing_notes}
                    )
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "task_rework",
                            "task_id": task["id"],
                            "notes": outcome.rework_notes,
                        }
                    )
                elif outcome.kind in (TaskOutcomeKind.BLOCKED, TaskOutcomeKind.FAILED):
                    self.store.update_task(task["id"], {"status": "blocked"})
                    meta.status = ProjectStatus.BLOCKED
                    self.store.write_meta(meta)
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "task_blocked" if outcome.kind == TaskOutcomeKind.BLOCKED else "task_failed",
                            "task_id": task["id"],
                            "reason": outcome.block_reason or outcome.failure_reason,
                        }
                    )
                    yield _event(
                        "task_blocked",
                        task_id=task["id"],
                        reason=outcome.block_reason or outcome.failure_reason,
                    )
                    return

                self.store.write_meta(meta)
                continue

        # Exceeded loop iteration safety cap — something is wrong.
        logger.error("Execution loop exceeded %d iterations; halting", _MAX_LOOP_ITERATIONS)
        meta = self.store.read_meta()
        meta.status = ProjectStatus.BLOCKED
        self.store.write_meta(meta)
        await self.store.append_decision(
            {
                "actor": "orchestrator",
                "kind": "loop_safety_halt",
                "reason": f"Execution loop ran {_MAX_LOOP_ITERATIONS} iterations without completing",
            }
        )
        yield _event("loop_safety_halt")

    # -----------------------------------------------------------------------------

    def _reset_abandoned_in_progress(self) -> None:
        """Restore tasks stuck in_progress from a prior crash to pending.

        If the backend died mid-task, that task's status is 'in_progress' but no one is
        working on it. Putting it back to 'pending' lets the scheduler pick it up again.
        The iteration count we bumped before running is preserved, so if we've been
        spinning on this task, we'll escalate sooner.
        """
        tasks = self.store.read_tasks()
        changed = False
        for t in tasks:
            if t.get("status") == "in_progress":
                t["status"] = "pending"
                t.setdefault("notes", []).append("Reset from in_progress after restart")
                changed = True
        if changed:
            self.store.write_tasks(tasks)


def _event(kind: str, **payload) -> StreamEvent:
    """Shorthand for emitting a loop-level event."""
    return StreamEvent(kind=kind, payload=payload)
