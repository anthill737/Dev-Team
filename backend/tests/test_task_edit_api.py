"""Tests for the per-task edit endpoints: PATCH /{id}/tasks/{tid} and
POST /{id}/tasks/bulk_budget.

Covers the critical contract:
  - Edit single task's budget_tokens
  - Append a user note to a task (prior notes preserved)
  - Bulk-update skips done tasks
  - Task not found → 404
  - Invalid budget (<=0) → 422
"""

from __future__ import annotations

import json
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.state import ProjectStore
from app.state.store import ProjectPhase


def _setup_tasks(tmp: str, project_id: str = "proj_task_edit") -> ProjectStore:
    store = ProjectStore(tmp)
    store.init(project_id=project_id, name="TaskEditTest")
    meta = store.read_meta()
    meta.phases = [ProjectPhase(id="P1", title="First", status="active", approved_by_user=True)]
    store.write_meta(meta)
    store.write_tasks([
        {
            "id": "P1-T1", "phase": "P1", "title": "Task 1", "description": "d",
            "acceptance_criteria": ["a"], "dependencies": [], "status": "pending",
            "assigned_to": "coder", "iterations": 0, "budget_tokens": 50_000,
            "notes": [], "requires_review": False, "review_cycles": 0,
        },
        {
            "id": "P1-T2", "phase": "P1", "title": "Task 2", "description": "d",
            "acceptance_criteria": ["a"], "dependencies": [], "status": "in_progress",
            "assigned_to": "coder", "iterations": 2, "budget_tokens": 50_000,
            "notes": ["prior note"], "requires_review": False, "review_cycles": 0,
        },
        {
            "id": "P1-T3", "phase": "P1", "title": "Task 3", "description": "d",
            "acceptance_criteria": ["a"], "dependencies": [], "status": "done",
            "assigned_to": "coder", "iterations": 1, "budget_tokens": 50_000,
            "notes": [], "requires_review": False, "review_cycles": 0,
        },
    ])
    return store


def _register_project(tmp: str, project_id: str = "proj_task_edit") -> None:
    from app.api.projects import _registry_path

    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if registry_path.exists():
        entries = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = [e for e in entries if e.get("id") != project_id]
    entries.append({"id": project_id, "name": "TaskEditTest", "root_path": tmp, "created_at": 1234567890.0})
    registry_path.write_text(json.dumps(entries), encoding="utf-8")


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_update_task_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T1",
            json={"budget_tokens": 200_000},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["budget_tokens"] == 200_000


def test_update_task_append_note_preserves_prior_notes() -> None:
    """User note append must not clobber existing notes — the Coder uses them
    to track prior iteration feedback."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T2",
            json={"add_note": "try using Playwright instead of Puppeteer"},
        )
        assert resp.status_code == 200
        notes = resp.json()["notes"]
        assert "prior note" in notes
        assert any("Playwright" in n for n in notes)


def test_update_task_budget_while_in_progress() -> None:
    """Core use case: bump budget on a task that's actively running. Must work
    regardless of status (the Coder will read the new value next iteration)."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T2",  # in_progress
            json={"budget_tokens": 500_000},
        )
        assert resp.status_code == 200
        assert resp.json()["budget_tokens"] == 500_000


def test_update_task_404_on_unknown_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-TNOPE",
            json={"budget_tokens": 100_000},
        )
        assert resp.status_code == 404


def test_update_task_rejects_zero_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T1",
            json={"budget_tokens": 0},
        )
        assert resp.status_code == 422


def test_bulk_budget_skips_done_tasks() -> None:
    """Bulk update must only touch non-done tasks — done ones have no work
    left to budget for."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post(
            "/api/projects/proj_task_edit/tasks/bulk_budget",
            json={"budget_tokens": 300_000},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["updated"] == 2  # T1 pending, T2 in_progress
        assert "P1-T1" in body["task_ids"]
        assert "P1-T2" in body["task_ids"]
        assert "P1-T3" not in body["task_ids"]  # done, skipped

        tasks = store.read_tasks()
        t1 = next(t for t in tasks if t["id"] == "P1-T1")
        t2 = next(t for t in tasks if t["id"] == "P1-T2")
        t3 = next(t for t in tasks if t["id"] == "P1-T3")
        assert t1["budget_tokens"] == 300_000
        assert t2["budget_tokens"] == 300_000
        assert t3["budget_tokens"] == 50_000  # unchanged


def test_bulk_budget_rejects_invalid_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post(
            "/api/projects/proj_task_edit/tasks/bulk_budget",
            json={"budget_tokens": -1},
        )
        assert resp.status_code == 422


# --- Interrupt tests --------------------------------------------------------


def test_interrupt_flips_task_to_review_and_project_to_awaiting() -> None:
    """Core interrupt behavior: note + interrupt=True halts the execution
    loop by flipping project to AWAITING_TASK_REVIEW and task to 'review'."""
    from app.state import ProjectStatus, ProjectStore

    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        # Set project to EXECUTING so interrupt has something to halt
        store = ProjectStore(tmp)
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)

        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T2",  # in_progress task
            json={"add_note": "stop and check with me", "interrupt": True},
        )
        assert resp.status_code == 200, resp.text

        # Re-read state
        store2 = ProjectStore(tmp)
        meta2 = store2.read_meta()
        assert meta2.status == ProjectStatus.AWAITING_TASK_REVIEW

        tasks = store2.read_tasks()
        t = next(t for t in tasks if t["id"] == "P1-T2")
        assert t["status"] == "review"
        assert t.get("interrupted_by_user") is True
        assert any("stop and check with me" in n for n in t["notes"])


def test_interrupt_ignored_on_done_tasks() -> None:
    """Interrupting a done task is pointless — the note should still be
    appended but the task must not flip to review."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T3",  # done task
            json={"add_note": "just FYI", "interrupt": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Note added but status stays done
        assert body["status"] == "done"
        assert body.get("interrupted_by_user") is not True


def test_interrupt_requires_note_to_be_meaningful() -> None:
    """interrupt=True with empty add_note shouldn't flip state — a naked
    interrupt with no message is useless."""
    from app.state import ProjectStatus, ProjectStore

    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        store = ProjectStore(tmp)
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)

        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T2",
            json={"add_note": "   ", "interrupt": True},
        )
        assert resp.status_code == 200

        meta2 = ProjectStore(tmp).read_meta()
        assert meta2.status == ProjectStatus.EXECUTING  # unchanged


def test_approve_on_interrupted_task_resumes_instead_of_marking_done() -> None:
    """User-interrupted tasks aren't 'done' on approve — the Coder didn't
    finish them. Approve resets to pending and clears the interrupt flag."""
    from app.state import ProjectStatus, ProjectStore

    with tempfile.TemporaryDirectory() as tmp:
        _setup_tasks(tmp)
        _register_project(tmp)
        client = _client_with_key()

        # Interrupt first
        r1 = client.patch(
            "/api/projects/proj_task_edit/tasks/P1-T2",
            json={"add_note": "pause here", "interrupt": True},
        )
        assert r1.status_code == 200

        # Now approve — should resume, not mark done
        r2 = client.post(
            "/api/projects/proj_task_edit/tasks/P1-T2/review",
            json={"approved": True},
        )
        assert r2.status_code == 200, r2.text

        store = ProjectStore(tmp)
        tasks = store.read_tasks()
        t = next(t for t in tasks if t["id"] == "P1-T2")
        assert t["status"] == "pending"  # ready to run again
        assert t.get("interrupted_by_user") is False
        meta = store.read_meta()
        assert meta.status == ProjectStatus.EXECUTING
        # tasks_completed must NOT have been incremented
        assert meta.tasks_completed == 0

