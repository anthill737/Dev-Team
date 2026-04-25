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
  5. If task has requires_review=True AND Coder signaled APPROVED, run the Reviewer.
     The Reviewer's verdict supersedes the Coder's — approve → task done; request_changes
     → convert to NEEDS_REWORK back to Coder; max cycles exhausted → task blocked.

The loop yields StreamEvents for live UI, same shape as the architect/dispatcher streams.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from ..agents.base import StreamEvent
from ..agents.reviewer import ReviewResult, Reviewer
from ..sandbox import SandboxExecutor
from ..state import ProjectStatus, ProjectStore
from .scheduler import SchedulerDecisionKind, choose_next_action
from .task_runner import TaskContext, TaskOutcome, TaskOutcomeKind, TaskRunner

logger = logging.getLogger(__name__)


# Safety caps on the loop itself, independent of per-task budgets. Prevents a bug in a
# task runner from looping forever even if nothing thinks the budget is exceeded.
_MAX_LOOP_ITERATIONS = 100

# Cap on how many times the Reviewer can send a task back to the Coder before it
# gets blocked for user intervention. Paper recommendation & our judgment: 2.
_MAX_REVIEW_CYCLES = 2


class ExecutionLoop:
    """Drives execution of an approved phase."""

    def __init__(
        self,
        store: ProjectStore,
        sandbox: SandboxExecutor,
        runner: TaskRunner,
        coder_model: str = "claude-sonnet-4-6",
        reviewer: Reviewer | None = None,
        reviewer_model: str = "claude-opus-4-7",
    ) -> None:
        self.store = store
        self.sandbox = sandbox
        self.runner = runner
        self._coder_model = coder_model
        # Reviewer is optional so tests that don't exercise the review path can pass
        # a scripted runner without needing to build a full Reviewer. Production
        # creation always passes one via the factory in websocket.py.
        self._reviewer = reviewer
        self._reviewer_model = reviewer_model

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
            # Read plan.md's phase list so the scheduler can tell "all tasks
            # done because the project is finished" (PROJECT_COMPLETE) from
            # "all tasks done because the Dispatcher only decomposed the
            # current phase and more phases exist in the plan" (PHASE_COMPLETE
            # → auto-advance fires). Cheap to recompute each iteration; plan.md
            # rarely changes during execution.
            phase_ids: list[str] = []
            try:
                from .phases import parse_phases as _parse_phases

                plan_text = self.store.read_plan()
                phase_ids = [p.id for p in _parse_phases(plan_text)]
            except Exception:  # noqa: BLE001
                # Fall back to None — scheduler reverts to old single-phase
                # behavior (treats all-tasks-done as PROJECT_COMPLETE).
                phase_ids = []

            decision = choose_next_action(
                tasks,
                current_phase=meta.current_phase,
                max_iterations=meta.max_task_iterations,
                remaining_phase_ids=phase_ids if phase_ids else None,
            )

            yield _event(
                "scheduler_decision",
                decision_kind=decision.kind.value,
                task_id=decision.task["id"] if decision.task else None,
                reason=decision.reason,
            )

            if decision.kind == SchedulerDecisionKind.PROJECT_COMPLETE:
                # Mark the current phase done too — PROJECT_COMPLETE fires
                # instead of PHASE_COMPLETE on the final phase, so we need to
                # close it out here. Without this, add-work after completion
                # sees the last phase in 'active' state, tries to reset and
                # re-dispatch it, and collides with the already-completed
                # tasks for that phase. (This was the NOTES-APP bug.)
                for p in meta.phases:
                    if p.id == meta.current_phase:
                        p.status = "done"
                meta.status = ProjectStatus.COMPLETE
                self.store.write_meta(meta)
                await self.store.append_decision(
                    {"actor": "orchestrator", "kind": "project_complete"}
                )
                yield _event("project_complete")
                return

            if decision.kind == SchedulerDecisionKind.PHASE_COMPLETE:
                # Mark the current phase done.
                for p in meta.phases:
                    if p.id == meta.current_phase:
                        p.status = "done"

                # Auto-advance to the next phase if plan.md has one. This
                # skips the old "bounce back to user / Architect" flow, which
                # was unnecessary — the Architect's job is *planning* phases;
                # *executing* them through the plan is Dispatcher territory.
                # The Architect only needs to come back if the plan itself
                # needs changes.
                #
                # If no next phase exists in plan.md, fall through to
                # PHASE_REVIEW (the user or scheduler will handle it — this
                # is the "plan had 1 phase and you finished it" scenario;
                # in practice the scheduler's PROJECT_COMPLETE branch catches
                # that before we get here, but keep the fallback as a safety).
                next_phase_id: str | None = None
                try:
                    from .phases import parse_phases as _parse_phases

                    plan_text = self.store.read_plan()
                    parsed = _parse_phases(plan_text)
                    # Build a map of what meta.phases says is done. Next phase
                    # is the lowest-numbered parsed phase whose id isn't done.
                    done_ids = {p.id for p in meta.phases if p.status == "done"}
                    for pp in parsed:
                        if pp.id not in done_ids:
                            next_phase_id = pp.id
                            break
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to parse plan.md for auto-advance; falling "
                        "back to PHASE_REVIEW."
                    )
                    next_phase_id = None

                if next_phase_id is not None:
                    # Ensure the phase row exists in meta.phases (old projects
                    # whose phases list may be out of sync with plan.md).
                    existing = next(
                        (p for p in meta.phases if p.id == next_phase_id), None
                    )
                    if existing is None:
                        from ..state.store import ProjectPhase

                        parsed_match = next(
                            (p for p in parsed if p.id == next_phase_id), None
                        )
                        meta.phases.append(
                            ProjectPhase(
                                id=next_phase_id,
                                title=(
                                    parsed_match.title if parsed_match else next_phase_id
                                ),
                                status="active",
                                approved_by_user=True,  # plan was already approved
                            )
                        )
                    else:
                        existing.status = "active"
                        existing.approved_by_user = True

                    meta.current_phase = next_phase_id
                    meta.status = ProjectStatus.DISPATCHING
                    self.store.write_meta(meta)
                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "phase_complete",
                            "phase": decision.phase_id if hasattr(decision, "phase_id") else None,
                            "auto_advance_to": next_phase_id,
                        }
                    )
                    yield _event(
                        "phase_auto_advance",
                        from_phase=decision.phase_id if hasattr(decision, "phase_id") else None,
                        to_phase=next_phase_id,
                    )
                    # Exit the execution loop so the outer stream_execution_loop
                    # can re-enter the DISPATCHING path and actually invoke the
                    # Dispatcher for the new phase. This is the only way to
                    # avoid duplicating the Dispatcher-launch logic inline here.
                    return

                # No next phase in plan → halt for user review.
                meta.status = ProjectStatus.PHASE_REVIEW
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
                # Fall back to the project's configured default if the task
                # somehow has no budget — avoids a hidden hardcoded floor that
                # silently overrode user settings.
                task_budget = min(
                    task.get("budget_tokens", meta.default_task_token_budget),
                    remaining,
                )
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
                    # Tag the event with its origin agent so downstream consumers
                    # (orchestrator's agent event buffer, frontend Inspector)
                    # know which agent to attribute it to. Adding an _agent key
                    # to the payload is non-destructive — existing consumers
                    # ignore unknown payload keys.
                    if "_agent" not in ev.payload:
                        ev.payload["_agent"] = "coder"
                        ev.payload["_task_id"] = task["id"]
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
                    # Reviewer gate: if this task is flagged for review and a Reviewer
                    # Reviewer policy: MANDATORY on every task. The per-task
                    # `requires_review` flag is ignored — user policy is that
                    # every task gets reviewed regardless of how the Dispatcher
                    # categorized it. Rationale: the Dispatcher was reasonably
                    # classifying scaffolding/utility tasks as not needing
                    # review (and was correct under the original policy), but
                    # the user found that errors in "low-stakes" tasks like
                    # PDF classification or filename parsing still needed to
                    # be caught — silent self-approval was missing real bugs.
                    #
                    # The only condition under which we skip the Reviewer now
                    # is if no Reviewer is configured at all (test fixtures
                    # that explicitly construct ExecutionLoop without one).
                    # In production, the orchestrator always provides a
                    # Reviewer, so this branch always fires.
                    review_result: ReviewResult | None = None
                    should_review = self._reviewer is not None

                    if should_review:
                        review_cycles_so_far = int(task.get("review_cycles", 0))
                        yield _event(
                            "review_start",
                            task_id=task["id"],
                            cycle=review_cycles_so_far + 1,
                            max_cycles=_MAX_REVIEW_CYCLES,
                        )
                        assert self._reviewer is not None  # narrowed by should_review
                        async for rev_ev in self._reviewer.run(
                            store=self.store,
                            sandbox=self.sandbox,
                            task={**task, "review_cycles": review_cycles_so_far},
                        ):
                            if rev_ev.kind == "review_outcome":
                                review_result = rev_ev.payload.get("result")
                            if "_agent" not in rev_ev.payload:
                                rev_ev.payload["_agent"] = "reviewer"
                                rev_ev.payload["_task_id"] = task["id"]
                            yield rev_ev

                        # Bill the Reviewer's tokens to the reviewer model bucket
                        if review_result is not None:
                            self.store.add_token_usage(
                                model=self._reviewer_model,
                                tokens_input=review_result.tokens_input,
                                tokens_output=review_result.tokens_output,
                                cache_read=review_result.cache_read_tokens,
                                cache_creation=review_result.cache_creation_tokens,
                            )
                            meta = self.store.read_meta()

                        if review_result is None or review_result.kind == "error":
                            # Reviewer crashed or timed out. Don't block on this —
                            # trust the Coder's APPROVED and move on. The error is
                            # logged; the user can always re-review manually later.
                            reason = (
                                review_result.error_reason
                                if review_result
                                else "Reviewer produced no result"
                            )
                            logger.warning(
                                "Reviewer error for task %s: %s — accepting Coder's approved",
                                task["id"],
                                reason,
                            )
                            await self.store.append_decision(
                                {
                                    "actor": "orchestrator",
                                    "kind": "review_error_passthrough",
                                    "task_id": task["id"],
                                    "reason": reason,
                                }
                            )
                            # Fall through to the task-approved branch below
                        elif review_result.kind == "approve":
                            await self.store.append_decision(
                                {
                                    "actor": "reviewer",
                                    "kind": "review_approved",
                                    "task_id": task["id"],
                                    "cycle": review_cycles_so_far + 1,
                                    "summary": review_result.summary,
                                }
                            )
                            yield _event(
                                "review_approved",
                                task_id=task["id"],
                                summary=review_result.summary,
                            )
                            # Fall through to the task-approved branch below
                        elif review_result.kind == "request_changes":
                            new_cycle_count = review_cycles_so_far + 1
                            await self.store.append_decision(
                                {
                                    "actor": "reviewer",
                                    "kind": "review_request_changes",
                                    "task_id": task["id"],
                                    "cycle": new_cycle_count,
                                    "summary": review_result.summary,
                                    "findings": list(review_result.findings),
                                }
                            )

                            if new_cycle_count >= _MAX_REVIEW_CYCLES:
                                # Bounded: max cycles exhausted. Block for user.
                                block_reason = (
                                    f"Reviewer rejected after {new_cycle_count} "
                                    f"cycle(s). Final findings: "
                                    + "; ".join(review_result.findings[:3])
                                )
                                self.store.update_task(
                                    task["id"],
                                    {
                                        "status": "blocked",
                                        "review_cycles": new_cycle_count,
                                        "review_findings": list(review_result.findings),
                                        "review_summary": review_result.summary,
                                    },
                                )
                                meta.status = ProjectStatus.BLOCKED
                                self.store.write_meta(meta)
                                yield _event(
                                    "task_blocked",
                                    task_id=task["id"],
                                    reason=block_reason,
                                )
                                return
                            else:
                                # Still have cycles left — send back to Coder with findings.
                                existing_notes = list(task.get("notes", []))
                                findings_note = (
                                    f"Reviewer (cycle {new_cycle_count}/"
                                    f"{_MAX_REVIEW_CYCLES}) requested changes: "
                                    f"{review_result.summary}\nFindings:\n"
                                    + "\n".join(f"  - {f}" for f in review_result.findings)
                                )
                                existing_notes.append(findings_note)
                                self.store.update_task(
                                    task["id"],
                                    {
                                        "status": "pending",
                                        "notes": existing_notes,
                                        "review_cycles": new_cycle_count,
                                    },
                                )
                                yield _event(
                                    "review_request_changes",
                                    task_id=task["id"],
                                    cycle=new_cycle_count,
                                    findings=list(review_result.findings),
                                )
                                self.store.write_meta(meta)
                                continue

                    # Task approved (either Coder alone, or Coder + Reviewer):
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

                    # Clean up Reviewer's scratch directory for this task. It's
                    # per-task so nothing useful survives and leftover scripts
                    # would clutter the project dir the user sees.
                    self._cleanup_review_scratch(task["id"])

                    await self.store.append_decision(
                        {
                            "actor": "orchestrator",
                            "kind": "task_approved",
                            "task_id": task["id"],
                            "summary": outcome.summary,
                            "reviewed": bool(should_review and review_result and review_result.kind == "approve"),
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

    def _cleanup_review_scratch(self, task_id: str) -> None:
        """Delete the Reviewer's scratch directory for a completed task.

        Failures here never block the task from being marked done — scratch files
        leftover is a cosmetic issue, not a correctness one. Logs a warning if
        something goes wrong so the cause is diagnosable.
        """
        import shutil
        from pathlib import Path

        scratch = Path(self.store.root) / ".devteam" / "review-scratch" / task_id
        try:
            if scratch.exists() and scratch.is_dir():
                shutil.rmtree(scratch)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to clean review scratch for task %s: %s", task_id, exc
            )


def _event(kind: str, **payload) -> StreamEvent:
    """Shorthand for emitting a loop-level event."""
    return StreamEvent(kind=kind, payload=payload)
