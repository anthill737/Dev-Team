"""Tests for read_task behavior — especially mid-task user edits.

Verifies:
  - read_task re-reads tasks.json fresh on each call (picks up PATCH edits
    made after the Coder started)
  - user_notes (entries prefixed "User note:") surface in their own field
  - Non-user notes stay under prior_notes
  - When no user notes exist, user_notes field is omitted (not empty list)
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any
from unittest.mock import MagicMock

from app.state import ProjectStore
from app.state.store import ProjectPhase
# Force agents.coder to load first so app.tools side imports complete cleanly
# before we reach in for build_coder_tools. Without this, tests/test_read_task
# run in isolation trip a circular import through orchestrator -> tools.
import app.agents.coder  # noqa: F401
from app.tools.coder_tools import build_coder_tools


def _setup_store(tmp: str, task_notes: list[str] | None = None) -> tuple[ProjectStore, dict[str, Any]]:
    store = ProjectStore(tmp)
    store.init(project_id="proj_read_task_test", name="Test")
    meta = store.read_meta()
    meta.phases = [ProjectPhase(id="P1", title="First", status="active", approved_by_user=True)]
    store.write_meta(meta)

    task: dict[str, Any] = {
        "id": "P1-T1",
        "phase": "P1",
        "title": "Build the thing",
        "description": "Task description",
        "acceptance_criteria": ["does X"],
        "dependencies": [],
        "status": "in_progress",
        "assigned_to": "coder",
        "iterations": 1,
        "budget_tokens": 100_000,
        "notes": task_notes or [],
        "requires_review": False,
        "review_cycles": 0,
    }
    store.write_tasks([task])
    return store, task


def _call_read_task(store: ProjectStore, task: dict[str, Any]) -> dict[str, Any]:
    """Build coder tools, grab read_task, run it, parse JSON result."""
    # SandboxExecutor is irrelevant for read_task; pass a mock
    tools = build_coder_tools(
        store=store,
        sandbox=MagicMock(),
        task=task,
        outcome_receiver=lambda _out: None,
    )
    read_task_tool = next(t for t in tools if t.name == "read_task")
    result = asyncio.run(read_task_tool.executor({}))
    return json.loads(result.content)


def test_read_task_splits_user_notes_from_prior_notes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store, task = _setup_store(
            tmp,
            task_notes=[
                "iteration 1 tried approach X",
                "User note: try using library Y instead",
                "rework: failed tests foo_test",
                "User note: stop and check with me before finalizing",
            ],
        )
        view = _call_read_task(store, task)
        assert view["user_notes"] == [
            "User note: try using library Y instead",
            "User note: stop and check with me before finalizing",
        ]
        assert view["prior_notes"] == [
            "iteration 1 tried approach X",
            "rework: failed tests foo_test",
        ]


def test_read_task_omits_user_notes_field_when_absent() -> None:
    """Prevent training the model to expect a sometimes-empty user_notes field."""
    with tempfile.TemporaryDirectory() as tmp:
        store, task = _setup_store(tmp, task_notes=["just a prior iteration note"])
        view = _call_read_task(store, task)
        assert "user_notes" not in view
        assert view["prior_notes"] == ["just a prior iteration note"]


def test_read_task_sees_notes_added_after_task_start() -> None:
    """Core mid-task edit case: task starts with no notes, user PATCHes a note
    via the UI, Coder calls read_task — the note must be visible.

    Before the fresh-read change, read_task used the closure-captured `task`
    snapshot and missed mid-task edits. This test would have failed prior to
    that fix.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store, task = _setup_store(tmp, task_notes=[])

        # Simulate the PATCH endpoint: append a user note to the task on disk
        store.update_task("P1-T1", {"notes": ["User note: pause and let me review"]})

        # Build coder tools BEFORE the note was added (to mirror task start),
        # then call read_task AFTER — the fresh-read should surface the note.
        # Actually _setup_store captures the snapshot; the update happens after
        # _setup_store returns. So `task` (passed to build_coder_tools) is the
        # pre-edit version, and the on-disk file has the post-edit version.
        view = _call_read_task(store, task)
        assert view.get("user_notes") == ["User note: pause and let me review"]


def test_read_task_falls_back_to_snapshot_if_task_missing_on_disk() -> None:
    """If somehow the task row isn't on disk (e.g., re-dispatched under a
    different id), don't crash — use the captured snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        store, task = _setup_store(tmp, task_notes=["original note"])
        # Overwrite tasks.json with a DIFFERENT task id to force fallback
        store.write_tasks([{**task, "id": "P1-OTHER"}])
        view = _call_read_task(store, task)
        # Falls back to the captured `task`, which still has id P1-T1
        assert view["id"] == "P1-T1"
        assert view["prior_notes"] == ["original note"]
