"""Tests for _recover_stuck_state — migrates projects broken by pre-fix bugs.

These tests simulate the two specific corruptions seen in practice:
  1. Single-phase project completed but phase still marked "active" in meta
  2. Add-work stuck in BLOCKED with current_phase pointing at a done phase

The function must be idempotent: running it again on a healthy project does
nothing.
"""

from __future__ import annotations

import tempfile

from app.api.projects import _recover_stuck_state
from app.state import ProjectStatus, ProjectStore
from app.state.store import ProjectPhase


def _make_task(tid: str, phase: str, status: str = "done") -> dict:
    return {
        "id": tid,
        "phase": phase,
        "title": "t",
        "description": "d",
        "acceptance_criteria": ["ok"],
        "dependencies": [],
        "status": status,
        "assigned_to": "coder",
        "iterations": 1,
        "budget_tokens": 50000,
        "notes": [],
        "requires_review": False,
        "review_cycles": 0,
    }


def test_recovery_marks_active_phase_done_when_all_tasks_done() -> None:
    """The single-phase-completion bug: phase is 'active' but all its tasks
    are 'done'. Recovery must flip the phase to 'done' so add-work doesn't
    try to redispatch it."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="Test")
        meta = store.read_meta()
        meta.phases = [
            ProjectPhase(id="P1", title="Phase 1", status="active", approved_by_user=True)
        ]
        meta.status = ProjectStatus.COMPLETE
        meta.current_phase = "P1"
        store.write_meta(meta)
        store.write_tasks([_make_task("P1-T1", "P1"), _make_task("P1-T2", "P1")])

        _recover_stuck_state(store)

        recovered = store.read_meta()
        assert recovered.phases[0].status == "done"


def test_recovery_advances_blocked_project_to_next_phase() -> None:
    """The NOTES-APP bug exactly: project is BLOCKED with current_phase=P1,
    P1 is actually done (all tasks complete), P2 is pending. Recovery must
    advance current_phase to P2 and set status to DISPATCHING."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="Test")
        meta = store.read_meta()
        meta.phases = [
            # P1 marked "active" (not done) despite all its tasks being done —
            # this is the pre-fix stuck state
            ProjectPhase(id="P1", title="Phase 1", status="active", approved_by_user=True),
            ProjectPhase(id="P2", title="Phase 2", status="pending", approved_by_user=False),
        ]
        meta.status = ProjectStatus.BLOCKED
        meta.current_phase = "P1"
        store.write_meta(meta)
        # All P1 tasks are done
        store.write_tasks([_make_task("P1-T1", "P1"), _make_task("P1-T2", "P1")])

        _recover_stuck_state(store)

        recovered = store.read_meta()
        assert recovered.phases[0].status == "done"  # P1 now done
        assert recovered.phases[1].status == "active"  # P2 activated
        assert recovered.phases[1].approved_by_user is True
        assert recovered.current_phase == "P2"
        assert recovered.status == ProjectStatus.DISPATCHING


def test_recovery_is_noop_on_healthy_project() -> None:
    """Running recovery on a correctly-structured project must not change
    anything. Idempotency check — this runs on every list_projects call."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="Test")
        meta = store.read_meta()
        meta.phases = [
            ProjectPhase(id="P1", title="Phase 1", status="active", approved_by_user=True)
        ]
        meta.status = ProjectStatus.EXECUTING
        meta.current_phase = "P1"
        store.write_meta(meta)
        # Tasks still in-progress — phase legitimately active
        store.write_tasks([
            _make_task("P1-T1", "P1", status="done"),
            _make_task("P1-T2", "P1", status="pending"),
        ])
        before = store.read_meta()

        _recover_stuck_state(store)

        after = store.read_meta()
        # Nothing should have changed
        assert after.status == before.status
        assert after.current_phase == before.current_phase
        assert after.phases[0].status == before.phases[0].status


def test_recovery_flips_to_complete_if_all_phases_done() -> None:
    """Edge case: project is BLOCKED but every phase is done. Recovery must
    flip to COMPLETE so the user sees it finished instead of stuck."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="Test")
        meta = store.read_meta()
        meta.phases = [
            ProjectPhase(id="P1", title="Phase 1", status="active", approved_by_user=True),
        ]
        meta.status = ProjectStatus.BLOCKED
        meta.current_phase = "P1"
        store.write_meta(meta)
        store.write_tasks([_make_task("P1-T1", "P1", status="done")])

        _recover_stuck_state(store)

        recovered = store.read_meta()
        assert recovered.status == ProjectStatus.COMPLETE
        assert recovered.current_phase is None
