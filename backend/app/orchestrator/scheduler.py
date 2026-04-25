"""Task scheduling logic for the execution loop.

Given the current state of tasks.json, decide what to do next:
  - Pick the next task whose dependencies are satisfied (preferring current phase).
  - Detect when all tasks in a phase are done (→ phase complete).
  - Detect deadlocks (tasks pending but none ready because deps are missing/broken).
  - Flag tasks that have exceeded their iteration budget (→ escalate).

This is pure logic: takes tasks as data, returns a decision. No network, no disk, no
agent calls. That makes it trivially testable against every interesting state shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SchedulerDecisionKind(str, Enum):
    RUN_TASK = "run_task"  # a task is ready; run it
    PHASE_COMPLETE = "phase_complete"  # all tasks in the current phase are done
    PROJECT_COMPLETE = "project_complete"  # every task is done
    DEADLOCK = "deadlock"  # pending tasks exist but none are ready (broken dep chain)
    ESCALATE_TASK = "escalate_task"  # task has exceeded iteration budget
    WAITING = "waiting"  # current task is in_progress or review — nothing to pick


@dataclass
class SchedulerDecision:
    kind: SchedulerDecisionKind
    task: dict[str, Any] | None = None
    reason: str = ""


# Hard cap. If a task has iterated this many times without reaching "done" it must
# be escalated — something is fundamentally wrong and spinning on it costs money
# without progress. Orchestrator-configured; this is the default fallback.
DEFAULT_MAX_ITERATIONS = 5


def choose_next_action(
    tasks: list[dict[str, Any]],
    current_phase: str | None,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    remaining_phase_ids: list[str] | None = None,
) -> SchedulerDecision:
    """Given the task list and the current phase, return the next action.

    Decision rules, in priority order:

      1. If any task has exceeded its iteration budget → ESCALATE_TASK.
      2. If any task is currently in_progress or review → WAITING (coder/reviewer owns it).
      3. If there's a pending task in the current phase whose deps are done → RUN_TASK.
      4. If every task in every phase is done AND no phases remain in the plan → PROJECT_COMPLETE.
      5. If every task in current phase is done (but others remain, OR more phases
         exist in plan.md that haven't been decomposed yet) → PHASE_COMPLETE.
      6. Otherwise, pending tasks exist but none are reachable → DEADLOCK.

    `remaining_phase_ids` should be the list of phase IDs from plan.md (e.g.
    ["P1", "P2", "P3"]). The Dispatcher decomposes one phase at a time, so when
    P1's tasks all finish, `tasks` only contains P1 tasks and the naive "all
    done" check would incorrectly return PROJECT_COMPLETE. By passing the full
    plan-phase list we can detect "the current phase is done but plan.md has
    more phases" and emit PHASE_COMPLETE so auto-advance can fire and the
    Dispatcher gets invoked for the next phase.

    If `remaining_phase_ids` is None, the scheduler falls back to the old
    behavior (assume the task list is complete). This preserves test fixtures
    that don't pass it.
    """
    if not tasks:
        # No tasks at all — the dispatcher hasn't run yet. This is a misuse of the scheduler;
        # surface it as waiting rather than claim completion.
        return SchedulerDecision(
            kind=SchedulerDecisionKind.WAITING,
            reason="No tasks yet — waiting for dispatcher",
        )

    # 1. Escalate any task that has burned through its iteration budget without reaching done
    for t in tasks:
        if t.get("status") != "done" and t.get("iterations", 0) >= max_iterations:
            return SchedulerDecision(
                kind=SchedulerDecisionKind.ESCALATE_TASK,
                task=t,
                reason=(
                    f"Task {t['id']} has iterated {t.get('iterations', 0)} times without "
                    f"completing (max {max_iterations})"
                ),
            )

    # 2. If the coder or reviewer is already mid-flight on a task, don't pick a new one.
    # The execution loop waits for that task to finish before coming back to the scheduler.
    for t in tasks:
        if t.get("status") in ("in_progress", "review"):
            return SchedulerDecision(
                kind=SchedulerDecisionKind.WAITING,
                task=t,
                reason=f"Task {t['id']} is {t['status']} — waiting for completion",
            )

    # 3. Find a ready task in the current phase
    done_ids = {t["id"] for t in tasks if t.get("status") == "done"}
    current_phase_tasks = [t for t in tasks if t.get("phase") == current_phase] if current_phase else tasks

    ready_in_phase = _find_ready(current_phase_tasks, done_ids)
    if ready_in_phase:
        return SchedulerDecision(
            kind=SchedulerDecisionKind.RUN_TASK,
            task=ready_in_phase,
            reason=f"Next ready task in {current_phase}",
        )

    # 4. Whole project complete? Check this BEFORE phase_complete so that when
    # everything is done we return PROJECT_COMPLETE, not PHASE_COMPLETE.
    #
    # CRITICAL nuance: "all tasks in tasks.json are done" is NOT the same as
    # "the project is complete". The Dispatcher decomposes one phase at a
    # time, so if we're partway through a multi-phase plan, tasks.json only
    # contains the current (and previously completed) phases. We need to
    # check plan.md's phase list — if there are phases beyond what we've
    # decomposed, the project isn't done; we just finished a phase.
    #
    # `remaining_phase_ids` tells us what plan.md says exist. If we know what
    # phases exist and the current phase is the last one, all-tasks-done
    # really does mean PROJECT_COMPLETE. Otherwise treat as PHASE_COMPLETE
    # so the auto-advance + Dispatcher can pick up the next phase.
    if all(t.get("status") == "done" for t in tasks):
        is_last_phase = (
            remaining_phase_ids is None
            or current_phase is None
            or current_phase == remaining_phase_ids[-1]
        )
        if is_last_phase:
            return SchedulerDecision(
                kind=SchedulerDecisionKind.PROJECT_COMPLETE,
                reason="All tasks done; current phase is the last in plan.md",
            )
        # More phases exist in plan.md beyond this one — emit PHASE_COMPLETE
        # so the orchestrator's auto-advance fires and the Dispatcher gets
        # called for the next phase.
        return SchedulerDecision(
            kind=SchedulerDecisionKind.PHASE_COMPLETE,
            reason=(
                f"All tasks in {current_phase} are done; plan.md has more phases "
                f"to decompose"
            ),
        )

    # 5. Current phase complete but other phases remain?
    if current_phase and all(
        t.get("status") == "done" for t in current_phase_tasks
    ) and current_phase_tasks:
        return SchedulerDecision(
            kind=SchedulerDecisionKind.PHASE_COMPLETE,
            reason=f"All tasks in {current_phase} are done",
        )

    # 6. We have pending tasks but no path forward — deadlock.
    pending = [t for t in tasks if t.get("status") not in ("done",)]
    unsatisfied = []
    for t in pending:
        missing = [d for d in t.get("dependencies", []) if d not in done_ids]
        if missing:
            unsatisfied.append(f"{t['id']} (missing: {', '.join(missing)})")
    return SchedulerDecision(
        kind=SchedulerDecisionKind.DEADLOCK,
        reason=(
            f"No tasks ready to run. Pending tasks with unmet dependencies: "
            f"{'; '.join(unsatisfied) if unsatisfied else '(none with deps — possibly a bug)'}"
        ),
    )


def _find_ready(tasks: list[dict[str, Any]], done_ids: set[str]) -> dict[str, Any] | None:
    """Return the first task whose status is 'pending' and all dependencies are done."""
    for t in tasks:
        if t.get("status") != "pending":
            continue
        if all(dep in done_ids for dep in t.get("dependencies", [])):
            return t
    return None
