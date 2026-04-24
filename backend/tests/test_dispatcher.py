"""Tests for the Dispatcher: schema validator, phase extraction, and tool behavior."""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from app.orchestrator.phases import parse_phases
from app.state import ProjectStatus, ProjectStore
from app.tools.dispatcher_tools import _validate_tasks, build_dispatcher_tools


# ---- Task schema validator ---------------------------------------------------


def test_validator_rejects_non_list() -> None:
    assert _validate_tasks("not a list") is not None  # type: ignore[arg-type]
    assert _validate_tasks({"not": "a list"}) is not None  # type: ignore[arg-type]


def test_validator_rejects_empty_list() -> None:
    err = _validate_tasks([])
    assert err is not None
    assert "empty" in err.lower()


def test_validator_rejects_too_many_tasks() -> None:
    tasks = [
        {
            "id": f"P1-T{i}",
            "phase": "P1",
            "title": f"Task {i}",
            "description": "x",
            "acceptance_criteria": ["it works"],
            "dependencies": [],
        }
        for i in range(60)
    ]
    err = _validate_tasks(tasks)
    assert err is not None
    assert "too many" in err.lower() or "60" in err


def test_validator_rejects_missing_required_fields() -> None:
    err = _validate_tasks([{"id": "P1-T1", "title": "ok"}])
    assert err is not None
    assert "required field" in err.lower() or "missing" in err.lower()


def test_validator_rejects_duplicate_ids() -> None:
    tasks = [
        _good_task("P1-T1"),
        _good_task("P1-T1"),  # dupe
    ]
    err = _validate_tasks(tasks)
    assert err is not None
    assert "duplicate" in err.lower()


def test_validator_rejects_empty_acceptance_criteria() -> None:
    t = _good_task("P1-T1")
    t["acceptance_criteria"] = []
    err = _validate_tasks([t])
    assert err is not None
    assert "acceptance" in err.lower()


def test_validator_rejects_empty_title() -> None:
    t = _good_task("P1-T1")
    t["title"] = "   "
    err = _validate_tasks([t])
    assert err is not None


def test_validator_accepts_well_formed_tasks() -> None:
    tasks = [
        _good_task("P1-T1"),
        _good_task("P1-T2", deps=["P1-T1"]),
        _good_task("P1-T3", deps=["P1-T1", "P1-T2"]),
    ]
    assert _validate_tasks(tasks) is None


def _good_task(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "phase": "P1",
        "title": "Do the thing",
        "description": "A task that does the thing.",
        "acceptance_criteria": [
            "The thing is observable",
            "A test demonstrates it",
        ],
        "dependencies": deps or [],
    }


# ---- Phase extraction from plan markdown -------------------------------------


def test_parse_phases_empty_plan() -> None:
    assert parse_phases("") == []


def test_parse_phases_no_phase_headings() -> None:
    plan = "# My Plan\n\nJust some text, no phase structure."
    assert parse_phases(plan) == []


def test_parse_phases_short_form() -> None:
    plan = """# Plan

## P1: Core gameplay
Ball, paddles, scoring.

## P2: Brainrot popups
The funny part.

## P3: Leaderboard
localStorage-based."""
    phases = parse_phases(plan)
    assert [p.id for p in phases] == ["P1", "P2", "P3"]
    assert phases[0].title == "Core gameplay"
    assert phases[1].title == "Brainrot popups"


def test_parse_phases_handles_em_dash_separator() -> None:
    # Real architect output uses em-dash; make sure we match it
    plan = "## P1 — Core gameplay\n\n## P2 — Leaderboard"
    phases = parse_phases(plan)
    assert len(phases) == 2
    assert phases[0].title == "Core gameplay"


# ---- Dispatcher tools end-to-end (integration) -------------------------------


def test_dispatcher_write_tasks_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        store.write_plan("## P1: Test phase\nStuff")

        tools = build_dispatcher_tools(store, phase_id="P1")
        write_tool = next(t for t in tools if t.name == "write_tasks")

        result = asyncio.run(
            write_tool.executor(
                {
                    "tasks": [
                        _good_task("P1-T1"),
                        _good_task("P1-T2", deps=["P1-T1"]),
                    ]
                }
            )
        )
        assert not result.is_error
        assert "2 tasks" in result.content

        tasks = store.read_tasks()
        assert len(tasks) == 2
        assert tasks[0]["status"] == "pending"
        assert tasks[0]["assigned_to"] == "coder"
        assert tasks[0]["budget_tokens"] == 150_000


def test_dispatcher_write_tasks_rejects_malformed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        tools = build_dispatcher_tools(store, phase_id="P1")
        write_tool = next(t for t in tools if t.name == "write_tasks")

        # Missing acceptance_criteria
        result = asyncio.run(
            write_tool.executor(
                {"tasks": [{"id": "P1-T1", "phase": "P1", "title": "ok", "description": "x"}]}
            )
        )
        assert result.is_error

        # Store should be unchanged
        assert store.read_tasks() == []


def test_dispatcher_write_tasks_detects_collision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        store.write_tasks([_good_task("P1-T1") | {"status": "pending"}])

        tools = build_dispatcher_tools(store, phase_id="P1")
        write_tool = next(t for t in tools if t.name == "write_tasks")

        result = asyncio.run(write_tool.executor({"tasks": [_good_task("P1-T1")]}))
        assert result.is_error
        assert "collision" in result.content.lower()


def test_dispatcher_mark_complete_requires_tasks_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        tools = build_dispatcher_tools(store, phase_id="P1")
        mark_tool = next(t for t in tools if t.name == "mark_dispatch_complete")

        result = asyncio.run(mark_tool.executor({"summary": "done"}))
        assert result.is_error
        assert "no tasks" in result.content.lower()


def test_dispatcher_mark_complete_transitions_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        # Set up DISPATCHING state
        meta = store.read_meta()
        meta.status = ProjectStatus.DISPATCHING
        store.write_meta(meta)

        store.write_tasks([_good_task("P1-T1") | {"status": "pending", "phase": "P1"}])

        tools = build_dispatcher_tools(store, phase_id="P1")
        mark_tool = next(t for t in tools if t.name == "mark_dispatch_complete")
        result = asyncio.run(mark_tool.executor({"summary": "P1 decomposed into 1 task"}))

        assert not result.is_error
        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.EXECUTING
        assert final_meta.current_phase == "P1"


def test_dispatcher_read_phase_extracts_section() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test")
        store.write_plan(
            "# Plan\n\n## P1: Foundation\n\nFoundation content.\n\n## P2: Features\n\nFeatures content."
        )
        tools = build_dispatcher_tools(store, phase_id="P1")
        read_phase = next(t for t in tools if t.name == "read_phase")

        result = asyncio.run(read_phase.executor({}))
        assert not result.is_error
        assert "Foundation" in result.content
        # P2 content should NOT leak in
        assert "Features content" not in result.content
