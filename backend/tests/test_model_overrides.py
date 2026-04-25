"""Tests for per-agent model overrides on project create/update."""

from __future__ import annotations

import json
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def _create_project(
    client: TestClient, tmp: str, name: str = "Test", **extra: Any
) -> dict[str, Any]:
    body = {"name": name, "root_path": tmp, **extra}
    resp = client.post("/api/projects", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_without_overrides_returns_global_defaults() -> None:
    """No model_* fields in request → ProjectDetail has the global defaults
    from config.Settings (Architect/Reviewer Opus, Dispatcher/Coder Sonnet)."""
    client = _client_with_key()
    with tempfile.TemporaryDirectory() as tmp:
        detail = _create_project(client, tmp)
        assert detail["model_architect"] == "claude-opus-4-7"
        assert detail["model_dispatcher"] == "claude-sonnet-4-6"
        assert detail["model_coder"] == "claude-sonnet-4-6"
        assert detail["model_reviewer"] == "claude-opus-4-7"


def test_create_with_overrides_applies_them() -> None:
    """All four overrides land in meta.json and come back via ProjectDetail."""
    client = _client_with_key()
    with tempfile.TemporaryDirectory() as tmp:
        detail = _create_project(
            client,
            tmp,
            name="OverrideTest",
            model_architect="claude-sonnet-4-6",  # downgrade from default Opus
            model_coder="claude-haiku-4-5-20251001",  # cheap mode
            model_reviewer="claude-sonnet-4-6",  # downgrade from default Opus
            # model_dispatcher omitted → stays at global default
        )
        assert detail["model_architect"] == "claude-sonnet-4-6"
        assert detail["model_coder"] == "claude-haiku-4-5-20251001"
        assert detail["model_reviewer"] == "claude-sonnet-4-6"
        assert detail["model_dispatcher"] == "claude-sonnet-4-6"  # default

        # Verify it survived the round trip via on-disk meta.json
        from pathlib import Path

        meta_path = Path(tmp) / ".devteam" / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["model_architect"] == "claude-sonnet-4-6"
        assert meta["model_coder"] == "claude-haiku-4-5-20251001"
        # Dispatcher in meta is None (not set); only resolved-detail fills it
        assert meta["model_dispatcher"] is None


def test_create_with_invalid_model_returns_400() -> None:
    """Bad model strings are rejected with a clear error."""
    client = _client_with_key()
    with tempfile.TemporaryDirectory() as tmp:
        body = {
            "name": "Bad",
            "root_path": tmp,
            "model_coder": "claude-totally-fake-model",
        }
        resp = client.post("/api/projects", json=body)
        assert resp.status_code == 400
        # The bad value should appear in the error message so user can correct
        assert "claude-totally-fake-model" in resp.text


def test_update_can_set_and_clear_override() -> None:
    """PATCH with a valid model sets the override; PATCH with sentinel
    'default' clears it back to the global default."""
    client = _client_with_key()
    with tempfile.TemporaryDirectory() as tmp:
        detail = _create_project(client, tmp)
        project_id = detail["id"]

        # Set Coder to Haiku
        resp = client.patch(
            f"/api/projects/{project_id}",
            json={"model_coder": "claude-haiku-4-5-20251001"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model_coder"] == "claude-haiku-4-5-20251001"

        # Clear it back to default with the sentinel
        resp = client.patch(
            f"/api/projects/{project_id}",
            json={"model_coder": "default"},
        )
        assert resp.status_code == 200, resp.text
        # Resolved value is the global default (Sonnet 4.6 for Coder)
        assert resp.json()["model_coder"] == "claude-sonnet-4-6"

        # On-disk meta has it cleared (None)
        from pathlib import Path
        meta = json.loads((Path(tmp) / ".devteam" / "meta.json").read_text())
        assert meta["model_coder"] is None


def test_update_with_invalid_model_returns_400_and_does_not_persist() -> None:
    """If one of four model fields is bad, none should be written. We validate
    all four before mutating anything."""
    client = _client_with_key()
    with tempfile.TemporaryDirectory() as tmp:
        detail = _create_project(client, tmp)
        project_id = detail["id"]

        # First override — valid
        resp = client.patch(
            f"/api/projects/{project_id}",
            json={"model_coder": "claude-haiku-4-5-20251001"},
        )
        assert resp.status_code == 200

        # Now an update with one bad field. Should reject without applying
        # ANY of the changes.
        resp = client.patch(
            f"/api/projects/{project_id}",
            json={
                "model_coder": "claude-sonnet-4-6",  # valid; would change
                "model_reviewer": "garbage-not-a-model",  # invalid
            },
        )
        assert resp.status_code == 400

        # Verify Coder is still Haiku (the invalid update should have aborted
        # all changes, not partially applied)
        from pathlib import Path
        meta = json.loads((Path(tmp) / ".devteam" / "meta.json").read_text())
        assert meta["model_coder"] == "claude-haiku-4-5-20251001"


def test_model_catalog_endpoint_returns_choices_and_defaults() -> None:
    """GET /api/projects/models/catalog returns the MODEL_CHOICES list and
    current global defaults so the UI doesn't have to hardcode either."""
    client = _client_with_key()
    resp = client.get("/api/projects/models/catalog")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Three known models in the catalog
    strings = {c["string"] for c in body["choices"]}
    assert "claude-opus-4-7" in strings
    assert "claude-sonnet-4-6" in strings
    assert "claude-haiku-4-5-20251001" in strings
    # Each entry has the three required fields
    for c in body["choices"]:
        assert {"string", "label", "cost_hint"} <= set(c.keys())
    # Defaults dict has all four roles
    assert set(body["defaults"].keys()) == {"architect", "dispatcher", "coder", "reviewer"}
    # Current defaults match what config.py says
    assert body["defaults"]["architect"] == "claude-opus-4-7"
    assert body["defaults"]["reviewer"] == "claude-opus-4-7"
    assert body["defaults"]["coder"] == "claude-sonnet-4-6"


def test_orchestrator_resolves_override_correctly() -> None:
    """The Orchestrator's _model_for helper reads meta override first, falls
    through to the global setting if no override is set."""
    from app.orchestrator import Orchestrator
    from app.state import ProjectStore

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        meta = store.read_meta()
        # Set one override, leave the rest None
        meta.model_coder = "claude-haiku-4-5-20251001"
        store.write_meta(meta)

        # Build orchestrator with a stub runner — never invoked, just type-shaped
        class _StubRunner:
            async def run(self, **_kwargs: Any) -> Any: ...
            async def stream(self, **_kwargs: Any) -> Any: ...

        orch = Orchestrator(store=store, runner=_StubRunner())  # type: ignore[arg-type]

        # Override applies
        assert orch._model_for("coder") == "claude-haiku-4-5-20251001"
        # Other roles fall through to global defaults
        assert orch._model_for("architect") == "claude-opus-4-7"
        assert orch._model_for("dispatcher") == "claude-sonnet-4-6"
        assert orch._model_for("reviewer") == "claude-opus-4-7"
