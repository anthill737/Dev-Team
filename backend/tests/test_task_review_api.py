"""Tests for the task user-review HTTP endpoint.

Covers the critical contract:
  - Approve from AWAITING_TASK_REVIEW → task done, project back to EXECUTING
  - Reject requires non-empty feedback
  - Reject → task back to pending with feedback in notes, project to EXECUTING
  - Can't review from wrong state (409)
  - Task not found (404)
  - Can't review a task that isn't in review state
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.state import ProjectStatus, ProjectStore
from app.state.store import ProjectPhase


def _setup_project_in_review(tmp: str, task_id: str = "P1-T1") -> ProjectStore:
    """Create a project with one task in review state, project awaiting review."""
    store = ProjectStore(tmp)
    store.init(project_id="proj_review_test", name="Test")
    store.write_tasks(
        [
            {
                "id": task_id,
                "phase": "P1",
                "title": "Build canvas",
                "description": "Canvas task",
                "acceptance_criteria": ["canvas renders"],
                "dependencies": [],
                "status": "review",
                "assigned_to": "coder",
                "iterations": 1,
                "budget_tokens": 50_000,
                "notes": [],
                "review_summary": "Built the canvas",
                "review_checklist": ["Open index.html", "Confirm canvas visible"],
                "review_run_command": "python -m http.server 8000",
                "review_files_to_check": ["index.html"],
                "review_requested_at": 1234567890.0,
            }
        ]
    )
    meta = store.read_meta()
    meta.status = ProjectStatus.AWAITING_TASK_REVIEW
    meta.current_phase = "P1"
    meta.phases = [
        ProjectPhase(id="P1", title="First", status="active", approved_by_user=True)
    ]
    store.write_meta(meta)
    return store


def _register_project(tmp: str, project_id: str = "proj_review_test") -> None:
    """Write the project registry so _load_store can find it."""
    from app.api.projects import _registry_path

    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    entries: list[dict[str, Any]] = []
    if registry_path.exists():
        entries = json.loads(registry_path.read_text(encoding="utf-8"))
    # Filter out any prior test entries with the same id to avoid stacking
    entries = [e for e in entries if e.get("id") != project_id]
    entries.append({"id": project_id, "root_path": tmp, "created_at": 1234567890.0})
    registry_path.write_text(json.dumps(entries), encoding="utf-8")


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_review_approve_transitions_task_to_done_and_project_to_executing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_review(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T1/review",
            json={"approved": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "executing"
        assert body["tasks_completed"] == 1

        store = ProjectStore(tmp)
        task = store.read_tasks()[0]
        assert task["status"] == "done"
        # The review_summary should have been promoted to the task's summary field,
        # so CompletedTasks renders it consistently
        assert task.get("summary") == "Built the canvas"
        assert "completed_at" in task

        decisions = store.read_decisions()
        assert any(
            d.get("kind") == "task_user_approved" and d.get("task_id") == "P1-T1"
            for d in decisions
        )


def test_review_reject_requires_non_empty_feedback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_review(tmp)
        _register_project(tmp)
        client = _client_with_key()

        # No feedback at all
        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T1/review",
            json={"approved": False},
        )
        assert resp.status_code == 400
        assert "feedback" in resp.json()["detail"].lower()

        # Whitespace-only feedback
        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T1/review",
            json={"approved": False, "feedback": "   "},
        )
        assert resp.status_code == 400


def test_review_reject_sends_task_back_to_pending_with_feedback_in_notes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_review(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T1/review",
            json={
                "approved": False,
                "feedback": "The canvas looks squashed — should be 960x540 but appears 480x270.",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

        store = ProjectStore(tmp)
        task = store.read_tasks()[0]
        assert task["status"] == "pending"
        # Feedback should be preserved in the notes so the Coder sees it on retry
        assert any("squashed" in n for n in task["notes"])
        # Review fields should be cleared to avoid stale state on next review
        assert task.get("review_summary") is None

        decisions = store.read_decisions()
        rejection = next(
            d for d in decisions if d.get("kind") == "task_user_rejected"
        )
        assert "squashed" in rejection["feedback"]


def test_review_fails_when_project_not_in_awaiting_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_project_in_review(tmp)
        # Put project back in EXECUTING (pretend something else moved it)
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)
        _register_project(tmp)

        client = _client_with_key()
        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T1/review",
            json={"approved": True},
        )
        assert resp.status_code == 409
        assert "awaiting_task_review" in resp.json()["detail"]


def test_review_fails_when_task_not_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_review(tmp)
        _register_project(tmp)
        client = _client_with_key()
        resp = client.post(
            "/api/projects/proj_review_test/tasks/P9-T99/review",
            json={"approved": True},
        )
        assert resp.status_code == 404


def test_review_fails_when_task_is_not_in_review_state() -> None:
    """If somehow the project is AWAITING_TASK_REVIEW but the caller names a different
    task that isn't in review, we must reject — don't silently approve the wrong task."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_project_in_review(tmp)
        # Add a second task that's still pending
        tasks = store.read_tasks()
        tasks.append(
            {
                "id": "P1-T2",
                "phase": "P1",
                "title": "Other task",
                "description": "x",
                "acceptance_criteria": ["y"],
                "dependencies": [],
                "status": "pending",
                "assigned_to": "coder",
                "iterations": 0,
                "budget_tokens": 50_000,
                "notes": [],
            }
        )
        store.write_tasks(tasks)
        _register_project(tmp)

        client = _client_with_key()
        resp = client.post(
            "/api/projects/proj_review_test/tasks/P1-T2/review",
            json={"approved": True},
        )
        assert resp.status_code == 409
        assert "review" in resp.json()["detail"].lower()
