"""Tests for POST /api/projects/{id}/force_submit_plan.

Backup path when the Architect gets stuck logging 'handing off' decision
entries instead of calling request_approval. User can force the transition.
"""

from __future__ import annotations

import json
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.state import ProjectStatus, ProjectStore
from app.state.store import ProjectPhase


def _setup_project_in_interview(
    tmp: str, plan_text: str, project_id: str = "proj_force_submit"
) -> ProjectStore:
    store = ProjectStore(tmp)
    store.init(project_id=project_id, name="Test")
    meta = store.read_meta()
    meta.status = ProjectStatus.INTERVIEW
    meta.phases = [
        ProjectPhase(id="P1", title="First", status="done", approved_by_user=True)
    ]
    store.write_meta(meta)
    if plan_text:
        store.write_plan(plan_text)
    return store


def _register_project(tmp: str, project_id: str = "proj_force_submit") -> None:
    from app.api.projects import _registry_path

    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if path.exists():
        entries = json.loads(path.read_text(encoding="utf-8"))
    entries = [e for e in entries if e.get("id") != project_id]
    entries.append(
        {"id": project_id, "name": "Test", "root_path": tmp, "created_at": 1234567890.0}
    )
    path.write_text(json.dumps(entries), encoding="utf-8")


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_force_submit_happy_path() -> None:
    """Project in INTERVIEW + plan.md has content → flips to AWAIT_APPROVAL."""
    plan = "# Plan\n\n## P1: First\n\nSome real content that's longer than 20 chars.\n\n## P2: Second\n\nMore stuff.\n"
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_interview(tmp, plan)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post("/api/projects/proj_force_submit/force_submit_plan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "await_approval"


def test_force_submit_refuses_empty_plan() -> None:
    """plan.md missing or too short → 400 (user must write the plan first)."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project_in_interview(tmp, plan_text="")  # no plan
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post("/api/projects/proj_force_submit/force_submit_plan")
        assert resp.status_code == 400
        assert "empty" in resp.text.lower() or "too short" in resp.text.lower()


def test_force_submit_refuses_from_wrong_state() -> None:
    """Only valid from INTERVIEW. Other states → 409."""
    plan = "# Plan\n\n## P1: First\n\nSome real content that's longer than 20 chars.\n"
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_project_in_interview(tmp, plan)
        # Flip to COMPLETE to simulate wrong state
        meta = store.read_meta()
        meta.status = ProjectStatus.COMPLETE
        store.write_meta(meta)

        _register_project(tmp)
        client = _client_with_key()

        resp = client.post("/api/projects/proj_force_submit/force_submit_plan")
        assert resp.status_code == 409


def test_force_submit_logs_decision() -> None:
    """A plan_force_submitted decision should be appended — this is noticeable
    in the UI and explains why the plan transitioned without the Architect."""
    plan = "# Plan\n\n## P1: First\n\nSome real content that's longer than 20 chars.\n"
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_project_in_interview(tmp, plan)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.post("/api/projects/proj_force_submit/force_submit_plan")
        assert resp.status_code == 200

        # Read decisions.log
        log_path = store.decisions_path
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert any(e.get("kind") == "plan_force_submitted" for e in entries)


def test_force_submit_404_on_unknown_project() -> None:
    client = _client_with_key()
    resp = client.post("/api/projects/proj_does_not_exist/force_submit_plan")
    assert resp.status_code == 404
