"""Tests for the retry_dispatcher orchestrator method and HTTP endpoint.

Contract:
  - From BLOCKED with current_phase set → flips status to DISPATCHING, logs
    a dispatcher_retry decision
  - From any other state → RuntimeError (409 at HTTP layer)
  - From BLOCKED without current_phase → RuntimeError
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.orchestrator import Orchestrator
from app.state import ProjectStatus, ProjectStore
from app.state.store import ProjectPhase


def _setup_blocked_project(tmp: str, project_id: str = "proj_retry_test") -> ProjectStore:
    """A project in BLOCKED state after a dispatcher failure, with current_phase set."""
    store = ProjectStore(tmp)
    store.init(project_id=project_id, name="Test")
    meta = store.read_meta()
    meta.status = ProjectStatus.BLOCKED
    meta.current_phase = "P1"
    meta.phases = [
        ProjectPhase(id="P1", title="Build MVP", status="active", approved_by_user=True)
    ]
    store.write_meta(meta)
    return store


def _register_project(tmp: str, project_id: str = "proj_retry_test") -> None:
    from app.api.projects import _registry_path

    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if registry_path.exists():
        entries = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = [e for e in entries if e.get("id") != project_id]
    entries.append({"id": project_id, "root_path": tmp, "created_at": 1.0})
    registry_path.write_text(json.dumps(entries), encoding="utf-8")


class _DummyRunner:
    """Placeholder runner — retry_dispatcher doesn't actually invoke the runner,
    it just flips status. Passing one to satisfy the Orchestrator constructor."""

    async def run(self, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, **_kwargs: Any) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_retry_dispatcher_flips_blocked_to_dispatching() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_blocked_project(tmp)
        orchestrator = Orchestrator(store=store, runner=_DummyRunner())

        await orchestrator.retry_dispatcher()

        meta = store.read_meta()
        assert meta.status == ProjectStatus.DISPATCHING
        assert meta.current_phase == "P1"

        decisions = store.read_decisions()
        retry_entries = [d for d in decisions if d.get("kind") == "dispatcher_retry"]
        assert len(retry_entries) == 1
        assert retry_entries[0]["actor"] == "user"
        assert retry_entries[0]["phase"] == "P1"


@pytest.mark.asyncio
async def test_retry_dispatcher_rejects_when_not_blocked() -> None:
    """Retry only makes sense from BLOCKED. From EXECUTING/DISPATCHING/etc it'd
    clobber in-flight work — refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_blocked_project(tmp)
        # Put it in EXECUTING instead
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)
        orchestrator = Orchestrator(store=store, runner=_DummyRunner())

        with pytest.raises(RuntimeError, match="not blocked"):
            await orchestrator.retry_dispatcher()


@pytest.mark.asyncio
async def test_retry_dispatcher_rejects_when_no_current_phase() -> None:
    """Edge case — BLOCKED but phase was never set. Shouldn't happen in practice
    but guard anyway."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_blocked_project(tmp)
        meta = store.read_meta()
        meta.current_phase = None
        store.write_meta(meta)
        orchestrator = Orchestrator(store=store, runner=_DummyRunner())

        with pytest.raises(RuntimeError, match="no current phase"):
            await orchestrator.retry_dispatcher()


def _client() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_retry_dispatcher_http_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _setup_blocked_project(tmp)
        _register_project(tmp)
        client = _client()

        resp = client.post("/api/projects/proj_retry_test/retry_dispatcher")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "dispatching"


def test_retry_dispatcher_http_409_when_not_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _setup_blocked_project(tmp)
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)
        _register_project(tmp)

        client = _client()
        resp = client.post("/api/projects/proj_retry_test/retry_dispatcher")
        assert resp.status_code == 409
        assert "not blocked" in resp.json()["detail"].lower()


# ---- API key bootstrap from env var ----------------------------------------------


def test_bootstrap_api_key_loads_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ANTHROPIC_API_KEY is set, startup should pre-populate the session store
    so the user doesn't have to enter it in the UI.

    Pinned to runner=api because in claude_code mode the bootstrap deliberately
    skips loading the key — that path has its own test below.
    """
    from app.main import _bootstrap_api_key_from_env
    from app.api.session import _store as session_store
    from app.config import get_settings

    session_store.clear()
    # Override runner on the cached singleton settings for this test. The
    # regex validator will reject anything other than claude_code|api.
    settings = get_settings()
    original_runner = settings.runner
    settings.runner = "api"
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-1234567890")
        _bootstrap_api_key_from_env()
        assert session_store.has() is True
        assert session_store.get() == "sk-ant-test-key-1234567890"
    finally:
        settings.runner = original_runner
        session_store.clear()


def test_bootstrap_skips_loading_key_in_claude_code_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In claude_code mode, the API key is ignored — loading it would give a
    misleading 'authenticated' state in the UI even though the key is never used."""
    from app.main import _bootstrap_api_key_from_env
    from app.api.session import _store as session_store
    from app.config import get_settings

    session_store.clear()
    settings = get_settings()
    original_runner = settings.runner
    settings.runner = "claude_code"
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-should-be-ignored")
        _bootstrap_api_key_from_env()
        assert session_store.has() is False  # not loaded
    finally:
        settings.runner = original_runner
        session_store.clear()


def test_bootstrap_api_key_ignores_missing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import _bootstrap_api_key_from_env
    from app.api.session import _store as session_store

    session_store.clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _bootstrap_api_key_from_env()
    assert session_store.has() is False


def test_bootstrap_api_key_rejects_malformed_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that doesn't start with sk-ant- is almost certainly a copy-paste mistake.
    We log a warning and fall through to the UI prompt rather than silently accepting
    a bad key."""
    from app.main import _bootstrap_api_key_from_env
    from app.api.session import _store as session_store

    session_store.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    _bootstrap_api_key_from_env()
    assert session_store.has() is False


def test_bootstrap_api_key_handles_whitespace_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty env var (e.g., ANTHROPIC_API_KEY=) should be treated as missing."""
    from app.main import _bootstrap_api_key_from_env
    from app.api.session import _store as session_store

    session_store.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    _bootstrap_api_key_from_env()
    assert session_store.has() is False
