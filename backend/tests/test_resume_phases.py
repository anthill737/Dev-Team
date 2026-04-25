"""Tests for the resume_phases endpoint — rescuing projects stuck at COMPLETE
with undone phases in plan.md (the multi-phase auto-advance bug)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.state import ProjectStore, ProjectStatus


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


_PLAN_THREE_PHASES = """\
# Test Project — Plan

## P1: Foundation
Goal: Set up project scaffolding.

Acceptance criteria:
- Vite + Three.js boots
- Tests pass

## P2: Combat
Goal: Add weapons and enemies.

Acceptance criteria:
- Weapons work
- Enemies pursue player

## P3: Polish
Goal: Audio, save/load, upgrades.

Acceptance criteria:
- localStorage round-trips
"""


def _setup_stuck_project(tmp: str, *, phases_done: list[str]) -> str:
    """Create a project that mimics the stuck state: status=COMPLETE, plan.md
    with three phases, only some marked done in meta. Returns project_id."""
    client = _client_with_key()
    resp = client.post(
        "/api/projects",
        json={"name": "StuckProject", "root_path": tmp},
    )
    assert resp.status_code == 200
    project_id = resp.json()["id"]

    # Write plan.md with all three phases
    store = ProjectStore(tmp)
    store.write_plan(_PLAN_THREE_PHASES)

    # Mutate meta to look like the stuck state: COMPLETE status, only
    # specified phases marked done
    meta = store.read_meta()
    from app.state import ProjectPhase

    meta.phases = [
        ProjectPhase(
            id=pid,
            title=f"Phase {pid}",
            status="done" if pid in phases_done else "pending",
        )
        for pid in ("P1", "P2", "P3")
    ]
    meta.status = ProjectStatus.COMPLETE
    meta.current_phase = phases_done[-1] if phases_done else None
    store.write_meta(meta)

    return project_id


def test_resume_phases_picks_first_undone_phase() -> None:
    """Stuck at COMPLETE with only P1 done. Resume should set current_phase=P2
    and flip status to DISPATCHING."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        project_id = _setup_stuck_project(tmp, phases_done=["P1"])

        resp = client.post(f"/api/projects/{project_id}/resume_phases")
        assert resp.status_code == 200, resp.text

        detail = resp.json()
        assert detail["status"] == "dispatching"
        # The detail should also reflect P2/P3 still unresolved
        assert "P2" in detail["unresolved_phase_ids"]
        assert "P3" in detail["unresolved_phase_ids"]

        # Verify on-disk meta matches
        meta = ProjectStore(tmp).read_meta()
        assert meta.status == ProjectStatus.DISPATCHING
        assert meta.current_phase == "P2"


def test_resume_phases_refuses_when_all_done() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        project_id = _setup_stuck_project(tmp, phases_done=["P1", "P2", "P3"])

        resp = client.post(f"/api/projects/{project_id}/resume_phases")
        assert resp.status_code == 400
        assert "already marked done" in resp.text.lower()


def test_resume_phases_refuses_when_running() -> None:
    """Don't race with an active worker. If status is DISPATCHING/EXECUTING
    we 409 and let the user pause first."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        project_id = _setup_stuck_project(tmp, phases_done=["P1"])
        # Force status to EXECUTING
        store = ProjectStore(tmp)
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)

        resp = client.post(f"/api/projects/{project_id}/resume_phases")
        assert resp.status_code == 409
        assert "currently" in resp.text.lower()


def test_resume_phases_refuses_when_phase_already_has_tasks() -> None:
    """If tasks.json has tasks for the phase we'd resume (partial dispatch
    left state behind), refuse and surface the conflict."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        project_id = _setup_stuck_project(tmp, phases_done=["P1"])
        # Inject pre-existing P2 tasks via the store API (which handles
        # path setup correctly even on freshly-created projects).
        store = ProjectStore(tmp)
        existing = store.read_tasks()
        existing.append(
            {
                "id": "P2-T1",
                "phase": "P2",
                "title": "Pre-existing P2 task",
                "description": "",
                "acceptance_criteria": [],
                "status": "pending",
                "deps": [],
            }
        )
        store.write_tasks(existing)

        resp = client.post(f"/api/projects/{project_id}/resume_phases")
        assert resp.status_code == 409
        assert "P2-T1" in resp.text


def test_unresolved_phase_ids_in_detail() -> None:
    """ProjectDetail.unresolved_phase_ids reflects what's left to do.
    Frontend uses this to show the Resume button."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        project_id = _setup_stuck_project(tmp, phases_done=["P1"])

        resp = client.get(f"/api/projects/{project_id}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["unresolved_phase_ids"] == ["P2", "P3"]


def test_unresolved_phase_ids_empty_when_no_plan() -> None:
    """Fresh project, no plan.md yet. unresolved_phase_ids should be empty
    list (not crash, not error)."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        resp = client.post(
            "/api/projects",
            json={"name": "Fresh", "root_path": tmp},
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["unresolved_phase_ids"] == []
