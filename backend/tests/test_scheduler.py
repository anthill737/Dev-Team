"""Tests for the task scheduler.

Each test specifies one scenario the execution loop will encounter in production, asserts
the correct decision. No implementation mirrors — tests describe states the user cares
about, not code structure.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.orchestrator.scheduler import (
    SchedulerDecisionKind,
    choose_next_action,
)


def task(
    task_id: str,
    *,
    phase: str = "P1",
    status: str = "pending",
    deps: list[str] | None = None,
    iterations: int = 0,
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
        "budget_tokens": 50_000,
        "notes": [],
    }


def test_empty_task_list_is_waiting_not_complete() -> None:
    """Before dispatcher runs, we shouldn't claim 'project complete'."""
    decision = choose_next_action([], current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.WAITING


def test_picks_first_pending_task_with_no_dependencies() -> None:
    tasks = [task("P1-T1"), task("P1-T2"), task("P1-T3")]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.RUN_TASK
    assert decision.task is not None
    assert decision.task["id"] == "P1-T1"


def test_skips_tasks_with_unsatisfied_dependencies() -> None:
    """T2 depends on T1; T1 is still pending. Scheduler picks T1, not T2."""
    tasks = [task("P1-T1"), task("P1-T2", deps=["P1-T1"])]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.RUN_TASK
    assert decision.task["id"] == "P1-T1"


def test_picks_task_whose_dependencies_are_done() -> None:
    """T1 is done; T2 depends on T1; T2 should now be ready."""
    tasks = [
        task("P1-T1", status="done"),
        task("P1-T2", deps=["P1-T1"]),
    ]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.RUN_TASK
    assert decision.task["id"] == "P1-T2"


def test_waiting_when_a_task_is_in_progress() -> None:
    """Coder is working; scheduler must not kick off another task in parallel."""
    tasks = [
        task("P1-T1", status="in_progress"),
        task("P1-T2"),  # would otherwise be ready
    ]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.WAITING
    assert decision.task["id"] == "P1-T1"


def test_waiting_when_a_task_is_in_review() -> None:
    """Reviewer is evaluating; similarly do not start another task."""
    tasks = [
        task("P1-T1", status="review"),
        task("P1-T2"),
    ]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.WAITING
    assert decision.task["id"] == "P1-T1"


def test_escalates_task_over_iteration_budget() -> None:
    """Task that has burned all its iterations must escalate, not loop forever."""
    tasks = [task("P1-T1", iterations=5)]
    decision = choose_next_action(tasks, current_phase="P1", max_iterations=5)
    assert decision.kind == SchedulerDecisionKind.ESCALATE_TASK
    assert decision.task["id"] == "P1-T1"


def test_does_not_escalate_done_task_over_budget() -> None:
    """A task that iterated a lot but ultimately succeeded shouldn't escalate retroactively.
    The escalation check must only fire on non-done tasks."""
    tasks = [
        task("P1-T1", status="done", iterations=10),
        task("P1-T2"),  # still pending — without this P1 would be fully complete
    ]
    decision = choose_next_action(tasks, current_phase="P1", max_iterations=5)
    # Critical assertion: NOT escalate. Instead, P1-T2 should be picked up next.
    assert decision.kind == SchedulerDecisionKind.RUN_TASK
    assert decision.task["id"] == "P1-T2"


def test_phase_complete_when_all_current_phase_tasks_done() -> None:
    """All P1 tasks done, but P2 tasks pending. Signal phase complete (not project)."""
    tasks = [
        task("P1-T1", status="done"),
        task("P1-T2", status="done"),
        task("P2-T1", phase="P2"),
    ]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.PHASE_COMPLETE


def test_project_complete_when_all_tasks_done() -> None:
    tasks = [
        task("P1-T1", status="done"),
        task("P2-T1", phase="P2", status="done"),
    ]
    # current_phase doesn't matter once everything's done, but we still supply one
    decision = choose_next_action(tasks, current_phase="P2")
    assert decision.kind == SchedulerDecisionKind.PROJECT_COMPLETE


def test_phase_complete_when_only_p1_decomposed_but_plan_has_more_phases() -> None:
    """Real-world bug: Dispatcher only decomposed P1's tasks. When P1 finishes,
    tasks.json contains only P1 tasks (all done). Without remaining_phase_ids,
    scheduler used to incorrectly emit PROJECT_COMPLETE — meaning auto-advance
    never fired and P2/P3/etc. were silently skipped. With remaining_phase_ids
    we know the plan has more phases and emit PHASE_COMPLETE so auto-advance
    can dispatch the next phase."""
    tasks = [
        task("P1-T1", status="done"),
        task("P1-T2", status="done"),
        # No P2 or P3 tasks yet — Dispatcher hasn't decomposed them.
    ]
    decision = choose_next_action(
        tasks,
        current_phase="P1",
        remaining_phase_ids=["P1", "P2", "P3"],
    )
    assert decision.kind == SchedulerDecisionKind.PHASE_COMPLETE
    assert "more phases" in decision.reason.lower()


def test_project_complete_when_current_phase_is_last_in_plan() -> None:
    """Same as the bug fix above, but the current phase IS the last one in
    plan.md. All tasks done + last phase = genuinely complete."""
    tasks = [
        task("P1-T1", status="done"),
        task("P2-T1", phase="P2", status="done"),
        task("P3-T1", phase="P3", status="done"),
    ]
    decision = choose_next_action(
        tasks,
        current_phase="P3",
        remaining_phase_ids=["P1", "P2", "P3"],
    )
    assert decision.kind == SchedulerDecisionKind.PROJECT_COMPLETE


def test_remaining_phase_ids_none_falls_back_to_old_behavior() -> None:
    """Tests that don't pass remaining_phase_ids should still work — backward
    compat for fixtures and any code path that doesn't have plan.md."""
    tasks = [task("P1-T1", status="done")]
    decision = choose_next_action(tasks, current_phase="P1")
    # Without plan info, we trust tasks.json says everything is done
    assert decision.kind == SchedulerDecisionKind.PROJECT_COMPLETE


def test_deadlock_when_pending_task_has_unreachable_dependency() -> None:
    """Task references a dep id that doesn't exist in the list. No escape route."""
    tasks = [task("P1-T1", deps=["does-not-exist"])]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.DEADLOCK
    assert "does-not-exist" in decision.reason


def test_deadlock_reason_names_the_problem_tasks() -> None:
    """The deadlock reason must help the user (or architect) understand what broke."""
    tasks = [
        task("P1-T1", deps=["ghost-a"]),
        task("P1-T2", deps=["ghost-b"]),
    ]
    decision = choose_next_action(tasks, current_phase="P1")
    assert decision.kind == SchedulerDecisionKind.DEADLOCK
    # Both problem tasks should appear in the reason
    assert "P1-T1" in decision.reason
    assert "P1-T2" in decision.reason


def test_prefers_current_phase_over_earlier_ready_task_in_other_phase() -> None:
    """If we're on P2, a ready task in P1 should be ignored in favor of P2 tasks.
    (Earlier phases should already be done; if they aren't, something went wrong and
    the user needs to resolve that — not the scheduler's job to mix phases.)"""
    tasks = [
        task("P1-T1", phase="P1"),  # pending P1 task
        task("P2-T1", phase="P2"),  # also pending, but we're on P2
    ]
    decision = choose_next_action(tasks, current_phase="P2")
    assert decision.kind == SchedulerDecisionKind.RUN_TASK
    assert decision.task["id"] == "P2-T1"


def test_in_progress_task_in_other_phase_still_blocks() -> None:
    """If somehow a task in another phase is in_progress, we still wait for it."""
    tasks = [
        task("P1-T1", phase="P1", status="in_progress"),
        task("P2-T1", phase="P2"),
    ]
    decision = choose_next_action(tasks, current_phase="P2")
    assert decision.kind == SchedulerDecisionKind.WAITING


def test_escalation_takes_priority_over_everything() -> None:
    """Even if another task is ready, an over-budget task must escalate first."""
    tasks = [
        task("P1-T1", iterations=10),  # way over budget
        task("P1-T2"),
    ]
    decision = choose_next_action(tasks, current_phase="P1", max_iterations=5)
    assert decision.kind == SchedulerDecisionKind.ESCALATE_TASK
    assert decision.task["id"] == "P1-T1"
