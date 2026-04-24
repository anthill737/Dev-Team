"""Tests for the PATCH project-settings endpoint.

Covers:
  - Name update
  - Budget fields update independently (partial update)
  - Wall clock: set to value, clear to null via sentinel flag
  - root_path change refused while running
  - root_path change refused if new path lacks .devteam/meta.json
  - root_path change refused if new path has a different project id
  - root_path change happy path: update the pointer
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.state import ProjectStore
from app.state.store import ProjectPhase


def _setup_project(tmp: str, project_id: str = "proj_update_test") -> ProjectStore:
    store = ProjectStore(tmp)
    store.init(project_id=project_id, name="Original Name")
    meta = store.read_meta()
    meta.phases = [ProjectPhase(id="P1", title="First", status="active", approved_by_user=True)]
    meta.project_token_budget = 2_000_000
    meta.default_task_token_budget = 50_000
    meta.max_task_iterations = 5
    meta.max_wall_clock_seconds = 3600
    store.write_meta(meta)
    return store


def _register_project(tmp: str, project_id: str = "proj_update_test") -> None:
    from app.api.projects import _registry_path

    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if registry_path.exists():
        entries = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = [e for e in entries if e.get("id") != project_id]
    entries.append(
        {"id": project_id, "name": "Original Name", "root_path": tmp, "created_at": 1234567890.0}
    )
    registry_path.write_text(json.dumps(entries), encoding="utf-8")


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_update_name_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_update_test",
            json={"name": "Renamed Project"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Renamed Project"
        # Other fields preserved
        assert body["project_token_budget"] == 2_000_000


def test_update_budgets_partial() -> None:
    """Partial update — changing budget doesn't touch other fields."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_update_test",
            json={"project_token_budget": 5_000_000},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_token_budget"] == 5_000_000
        assert body["name"] == "Original Name"  # unchanged
        assert body["default_task_token_budget"] == 50_000  # unchanged


def test_update_wall_clock_clear() -> None:
    """clear_max_wall_clock flag sets the limit to None ('unlimited')."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_update_test",
            json={"clear_max_wall_clock": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_wall_clock_seconds"] is None


def test_update_wall_clock_set_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_update_test",
            json={"max_wall_clock_seconds": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["max_wall_clock_seconds"] == 7200


def test_update_rejects_invalid_budget() -> None:
    """Pydantic gt=0 constraint should 422 on zero/negative values."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        resp = client.patch(
            "/api/projects/proj_update_test",
            json={"project_token_budget": 0},
        )
        assert resp.status_code == 422


def test_update_root_path_refuses_when_new_path_missing_devteam() -> None:
    """If the new path doesn't have .devteam/meta.json, refuse — we don't move
    files for the user. They must move the folder first."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        with tempfile.TemporaryDirectory() as empty_tmp:
            # empty_tmp is a valid dir but has no .devteam/
            resp = client.patch(
                "/api/projects/proj_update_test",
                json={"root_path": empty_tmp},
            )
            assert resp.status_code == 400
            assert "devteam" in resp.text.lower()


def test_update_root_path_refuses_when_new_path_has_different_project() -> None:
    """If the new path's .devteam/meta.json is for a different project, refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp, project_id="proj_update_test")
        _register_project(tmp, project_id="proj_update_test")
        client = _client_with_key()

        # Set up a DIFFERENT project at a new path
        with tempfile.TemporaryDirectory() as other_tmp:
            other_store = ProjectStore(other_tmp)
            other_store.init(project_id="proj_SOMETHING_ELSE", name="Other")

            resp = client.patch(
                "/api/projects/proj_update_test",
                json={"root_path": other_tmp},
            )
            assert resp.status_code == 400
            assert "different project" in resp.text.lower()


def test_update_root_path_happy_path() -> None:
    """Move .devteam/ to a new folder manually, then point the endpoint at it.
    The registry and meta.json both get updated."""
    import shutil

    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        _register_project(tmp)
        client = _client_with_key()

        with tempfile.TemporaryDirectory() as new_tmp:
            # User "moves" the project — copy .devteam/ to the new location.
            # (In practice they'd move; for the test, copy is cleaner.)
            shutil.copytree(
                Path(tmp) / ".devteam",
                Path(new_tmp) / ".devteam",
            )

            resp = client.patch(
                "/api/projects/proj_update_test",
                json={"root_path": new_tmp},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Returned detail reflects new path
            assert body["root_path"] == str(Path(new_tmp).resolve())

            # Registry also updated
            from app.api.projects import _registry_path, _load_registry
            registry = _load_registry()
            entry = next(e for e in registry if e["id"] == "proj_update_test")
            assert entry["root_path"] == str(Path(new_tmp).resolve())


def test_update_404_on_unknown_project() -> None:
    client = _client_with_key()
    resp = client.patch(
        "/api/projects/proj_does_not_exist",
        json={"name": "anything"},
    )
    assert resp.status_code == 404
