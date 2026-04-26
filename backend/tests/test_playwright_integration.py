"""Tests for the playwright_enabled flag end-to-end.

Covers:
  - Meta round-trips the field correctly
  - Create endpoint accepts and persists the flag
  - Update endpoint toggles it; None means "no change"
  - ProjectDetail surfaces the resolved value
  - build_reviewer_tools registers playwright_check only when enabled
  - reviewer_prompt renders different Step 0 blocks per mode
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.session import _store as _key_store
from app.main import app
from app.prompts.library import reviewer_prompt
from app.state import ProjectStore


def _client_with_key() -> TestClient:
    _key_store.set("sk-ant-fake-for-testing")
    return TestClient(app)


def test_meta_roundtrips_playwright_enabled() -> None:
    """Write meta with playwright_enabled set, read it back, value matches."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="t")
        meta = store.read_meta()
        assert meta.playwright_enabled is False  # default

        meta.playwright_enabled = True
        store.write_meta(meta)

        re_read = store.read_meta()
        assert re_read.playwright_enabled is True


def test_create_project_with_playwright_enabled() -> None:
    """Create endpoint accepts the flag and persists it."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        resp = client.post(
            "/api/projects",
            json={
                "name": "PWTest",
                "root_path": tmp,
                "playwright_enabled": True,
            },
        )
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["playwright_enabled"] is True

        # And on disk
        meta = ProjectStore(tmp).read_meta()
        assert meta.playwright_enabled is True


def test_create_project_default_playwright_off() -> None:
    """Default is False if not specified — opt-in only."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        resp = client.post(
            "/api/projects",
            json={"name": "PWTestDefault", "root_path": tmp},
        )
        assert resp.status_code == 200
        assert resp.json()["playwright_enabled"] is False


def test_update_endpoint_toggles_playwright() -> None:
    """PATCH the project to flip playwright on, then off. None on update
    means 'don't change' — a separate test confirms that."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        create = client.post(
            "/api/projects",
            json={"name": "Toggle", "root_path": tmp},
        )
        pid = create.json()["id"]

        # Flip on
        on = client.patch(
            f"/api/projects/{pid}",
            json={"playwright_enabled": True},
        )
        assert on.status_code == 200, on.text
        assert on.json()["playwright_enabled"] is True

        # Flip off
        off = client.patch(
            f"/api/projects/{pid}",
            json={"playwright_enabled": False},
        )
        assert off.status_code == 200
        assert off.json()["playwright_enabled"] is False


def test_update_with_none_does_not_change_playwright() -> None:
    """Omitting playwright_enabled (or sending None) leaves it as-is —
    important for partial PATCH behavior. The user might be updating only
    the project name and shouldn't have their playwright setting reset."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_with_key()
        create = client.post(
            "/api/projects",
            json={
                "name": "NoChange",
                "root_path": tmp,
                "playwright_enabled": True,
            },
        )
        pid = create.json()["id"]

        # Update only the name
        resp = client.patch(
            f"/api/projects/{pid}",
            json={"name": "Renamed"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"
        # Playwright still on
        assert resp.json()["playwright_enabled"] is True


def test_build_reviewer_tools_registers_playwright_check_when_enabled() -> None:
    """The playwright_check tool appears in the tool list iff enabled."""
    from app.tools.reviewer_tools import build_reviewer_tools
    from app.sandbox import ProcessSandboxExecutor

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p1", name="t")
        sandbox = ProcessSandboxExecutor(project_root=Path(tmp))
        task = {
            "id": "P1-T1",
            "phase": "P1",
            "title": "x",
            "description": "x",
            "acceptance_criteria": [],
        }
        no_op_receiver = lambda _sig: None  # noqa: E731

        # Disabled
        off_tools = build_reviewer_tools(
            store=store,
            sandbox=sandbox,
            task=task,
            signal_receiver=no_op_receiver,
            playwright_enabled=False,
        )
        off_names = {t.name for t in off_tools}
        assert "playwright_check" not in off_names
        # Standard tools still present
        assert "submit_review" in off_names
        assert "bash" in off_names

        # Enabled
        on_tools = build_reviewer_tools(
            store=store,
            sandbox=sandbox,
            task=task,
            signal_receiver=no_op_receiver,
            playwright_enabled=True,
        )
        on_names = {t.name for t in on_tools}
        assert "playwright_check" in on_names
        assert "submit_review" in on_names


def test_reviewer_prompt_includes_step_zero_when_playwright_on() -> None:
    on = reviewer_prompt(user_platform="linux", playwright_enabled=True)
    off = reviewer_prompt(user_platform="linux", playwright_enabled=False)

    assert "Playwright is ENABLED" in on
    assert "playwright_check" in on
    assert "Playwright is DISABLED" in off
    assert "playwright_check" not in off

    # The three hard rules are present in both — Step 0 only changes the
    # verification path, not the rest of the framing.
    assert "RULE 1" in on
    assert "RULE 1" in off


def test_reviewer_prompt_tells_reviewer_to_self_install_playwright() -> None:
    """When playwright_check returns 'not installed', the Reviewer should
    install it via bash, not request_changes against the Coder. Pin the
    self-install language so future prompt edits can't quietly regress
    this and put us back to 'ask the user' behavior."""
    on = reviewer_prompt(user_platform="linux", playwright_enabled=True)
    # The prompt must explicitly tell the model how to install
    assert "pip" in on and "install" in on and "playwright" in on
    assert "chromium" in on
    # And it must explicitly distance this from request_changes — a missing
    # Playwright is a bootstrap step, not a Coder defect.
    assert "bootstrap" in on.lower() or "not a code defect" in on.lower()


def test_reviewer_prompt_default_is_disabled() -> None:
    """Backward compatibility: existing call sites without the flag get the
    'disabled' branch, which is also the safe default."""
    p = reviewer_prompt(user_platform="linux")
    assert "Playwright is DISABLED" in p


def test_coder_prompt_routes_visual_tasks_to_reviewer_when_playwright_on() -> None:
    """The actual fix for 'Reviewer never runs'. With Playwright off, the
    Coder is told to route visual tasks to needs_user_review (which bypasses
    the Reviewer entirely). With Playwright on, the Coder should default to
    approved on visual tasks too — Reviewer can verify them via
    playwright_check. This test pins that contract.

    Without this guard, a future prompt edit could revert to "always escalate
    UI to user" and the Reviewer would silently never run on visual projects.
    """
    from app.prompts.library import coder_prompt

    on = coder_prompt(user_platform="linux", playwright_enabled=True)
    off = coder_prompt(user_platform="linux", playwright_enabled=False)

    # Playwright on: Coder is told the Reviewer can verify visuals
    assert "playwright_check" in on
    assert "Default to approved for browser-rendered work" in on

    # Playwright off: Coder must escalate visuals to the user
    assert "MUST be needs_user_review" in off
    # And shouldn't be confused by Playwright references it can't use
    assert "playwright_check" not in off

    # The "green tests, black screen" warning should be preserved in the
    # off-branch as the rationale for escalation. (In on-branch, the
    # Reviewer's prompt carries this warning instead.)
    assert "green tests" in off or "black screen" in off
