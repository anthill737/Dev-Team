"""End-to-end integration test for the dispatcher flow.

Uses a fake AgentRunner that simulates a realistic dispatcher turn — reads the phase,
writes tasks, marks complete. Exercises the full wiring: approve_plan → phase parsing
→ dispatcher streaming → tool execution → status transitions.

The only thing this doesn't verify is the actual Claude model behavior (would need a
real API key and network). Everything else — the harness, orchestrator, tools, state
transitions — is fully covered.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any, AsyncIterator, Callable

import pytest

from app.agents.base import (
    AgentRunResult,
    AgentRunner,
    Message,
    StreamEvent,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from app.orchestrator import Orchestrator
from app.state import ProjectStatus, ProjectStore


# ---- A fake runner that executes a scripted sequence of "tool calls" -------------------------
#
# Real AgentRunner: Claude model sees the prompt, calls tools, accumulates response.
# Fake runner: we give it a script of {tool_name, tool_input} tuples to execute in order.
# This lets us verify that the dispatcher integration correctly threads tools, state
# transitions, and token accounting — without actually hitting the API.


class ScriptedRunner:
    """Plays a pre-recorded sequence of tool calls as if a model had made them."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]], final_text: str = "Done.") -> None:
        self._script = script
        self._final_text = final_text

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        raise NotImplementedError("ScriptedRunner uses stream() only")

    async def stream(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int = 4096,
        max_iterations: int = 20,
        wall_clock_seconds: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        tool_map = {t.name: t.executor for t in tools}
        tool_calls = 0

        for i, (tool_name, tool_input) in enumerate(self._script):
            if tool_name not in tool_map:
                yield StreamEvent(
                    kind="error",
                    payload={"message": f"Script referenced unknown tool: {tool_name}"},
                )
                return

            yield StreamEvent(
                kind="tool_use_start",
                payload={"name": tool_name, "input": tool_input},
            )

            result = await tool_map[tool_name](tool_input)
            tool_calls += 1

            yield StreamEvent(
                kind="tool_result",
                payload={
                    "name": tool_name,
                    "is_error": result.is_error,
                    "content_preview": str(result.content)[:200],
                },
            )

            # Stop early if a tool returned an error (simulates model giving up)
            if result.is_error:
                break

        yield StreamEvent(
            kind="text_delta",
            payload={"text": self._final_text},
        )

        # Synthesize a completion event with realistic-looking usage
        yield StreamEvent(
            kind="usage",
            payload={"input_tokens": 1000 * len(self._script), "output_tokens": 500 * len(self._script)},
        )

        yield StreamEvent(
            kind="turn_complete",
            payload={
                "result": AgentRunResult(
                    final_text=self._final_text,
                    messages=[],
                    tokens_input=1000 * len(self._script),
                    tokens_output=500 * len(self._script),
                    stop_reason="end_turn",
                    tool_calls_made=tool_calls,
                )
            },
        )


# ---- Test fixtures --------------------------------------------------------------------------


SAMPLE_PLAN = """# Brainrot Pong — Plan

## Project Summary
Browser-based Pong clone with Gen Z brainrot slang popups.

## MVP Scope
Classic pong + slang popups + local leaderboard.

## P1: Core gameplay
- Canvas rendering
- Ball physics
- Paddle controls
- Scoring

## P2: Brainrot popups
- Slang list
- Popup spawning
- Fade animation

## P3: Leaderboard
- localStorage
- Top 10 display
"""


def _well_formed_tasks() -> list[dict[str, Any]]:
    return [
        {
            "id": "P1-T1",
            "phase": "P1",
            "title": "Set up HTML canvas and game loop",
            "description": "Create index.html, set up 960x540 canvas, requestAnimationFrame loop.",
            "acceptance_criteria": [
                "index.html loads in a browser and shows a 960x540 canvas",
                "Game loop runs at ~60fps using requestAnimationFrame",
                "Clear frame is drawn each tick",
            ],
            "dependencies": [],
        },
        {
            "id": "P1-T2",
            "phase": "P1",
            "title": "Implement ball physics and paddle rendering",
            "description": "Ball bounces off walls and paddles; paddles render on left and right.",
            "acceptance_criteria": [
                "Ball moves at constant velocity and reverses direction on wall collision",
                "Both paddles render at correct vertical positions",
                "Ball-paddle collision reverses horizontal velocity",
            ],
            "dependencies": ["P1-T1"],
        },
        {
            "id": "P1-T3",
            "phase": "P1",
            "title": "Add player controls and scoring",
            "description": "Arrow keys and W/S move player paddle; score increments on miss.",
            "acceptance_criteria": [
                "Arrow up/down and W/S both move the player paddle",
                "Ball passing behind a paddle increments the opposing score",
                "Score renders at the top of the canvas",
                "First to 7 triggers end-of-match state",
            ],
            "dependencies": ["P1-T2"],
        },
    ]


# ---- The integration test --------------------------------------------------------------------


def test_full_dispatcher_flow_with_scripted_runner() -> None:
    """The complete path: approved plan → dispatcher runs → tasks populated → status ready."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_e2e", name="Pong test")
        store.write_plan(SAMPLE_PLAN)

        # Put project in AWAIT_APPROVAL so approve_plan() can transition it
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)

        # Script the dispatcher's expected behavior: read the phase, write tasks, mark complete.
        script = [
            ("read_phase", {}),
            ("write_tasks", {"tasks": _well_formed_tasks()}),
            ("mark_dispatch_complete", {"summary": "P1 decomposed into 3 tasks"}),
        ]
        runner = ScriptedRunner(script, final_text="Phase 1 decomposed.")

        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        # 1. User approves the plan
        phase_id = asyncio.run(orchestrator.approve_plan())
        assert phase_id == "P1"

        meta_after_approve = store.read_meta()
        assert meta_after_approve.status == ProjectStatus.DISPATCHING
        assert len(meta_after_approve.phases) == 3
        assert [p.id for p in meta_after_approve.phases] == ["P1", "P2", "P3"]
        assert meta_after_approve.phases[0].status == "active"
        assert meta_after_approve.phases[0].approved_by_user is True
        assert meta_after_approve.current_phase == "P1"

        # 2. Dispatcher streams its run
        async def consume_stream() -> list[StreamEvent]:
            events = []
            async for ev in orchestrator.stream_dispatcher_turn():
                events.append(ev)
            return events

        events = asyncio.run(consume_stream())

        # We should see tool_use, tool_result, text_delta, usage, turn_complete events
        kinds = [e.kind for e in events]
        assert "tool_use_start" in kinds
        assert "tool_result" in kinds
        assert "turn_complete" in kinds

        # 3. Post-dispatch state verification
        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.EXECUTING, (
            f"Expected EXECUTING after dispatch, got {final_meta.status}"
        )
        assert final_meta.current_phase == "P1"

        # Tokens should have been tracked
        assert final_meta.tokens_used > 0

        # 4. Tasks should be in tasks.json
        tasks = store.read_tasks()
        assert len(tasks) == 3
        task_ids = [t["id"] for t in tasks]
        assert task_ids == ["P1-T1", "P1-T2", "P1-T3"]

        # Every task should have been normalized with defaults
        for t in tasks:
            assert t["status"] == "pending"
            assert t["assigned_to"] == "coder"
            assert t["budget_tokens"] == 150_000
            assert t["iterations"] == 0
            assert len(t["acceptance_criteria"]) > 0

        # 5. Decisions log should have the audit trail
        decisions = store.read_decisions()
        decision_kinds = [d["kind"] for d in decisions]
        assert "plan_approved" in decision_kinds
        assert "dispatcher_start" in decision_kinds
        assert "tasks_written" in decision_kinds
        assert "dispatch_complete" in decision_kinds


def test_dispatcher_handles_malformed_tasks_and_blocks() -> None:
    """If the 'model' writes bad tasks and gives up, we should land in BLOCKED, not stuck."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_e2e", name="Pong test")
        store.write_plan(SAMPLE_PLAN)
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)

        # Script: dispatcher tries to write malformed tasks, fails validation,
        # then never retries or completes. This simulates a model giving up.
        script = [
            ("read_phase", {}),
            (
                "write_tasks",
                {
                    "tasks": [
                        {
                            "id": "P1-T1",
                            "phase": "P1",
                            "title": "",  # EMPTY — will fail validation
                            "description": "x",
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ]
                },
            ),
            # Note: no mark_dispatch_complete — simulates model giving up after error
        ]
        runner = ScriptedRunner(script)

        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]
        asyncio.run(orchestrator.approve_plan())

        async def consume() -> None:
            async for _ in orchestrator.stream_dispatcher_turn():
                pass

        asyncio.run(consume())

        # Because dispatcher stream ended without mark_dispatch_complete, orchestrator
        # should have moved status to BLOCKED rather than leaving it in DISPATCHING.
        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.BLOCKED

        # No tasks should have been written (validation rejected them)
        assert store.read_tasks() == []

        # But the error should be in the decisions log for the user to see
        decisions = store.read_decisions()
        assert any(d["kind"] == "dispatcher_blocked" for d in decisions)


def test_plan_without_phase_headings_falls_back_gracefully() -> None:
    """Some plans won't have clean P1/P2 headings. We fall back to a single P1 phase."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_e2e", name="Minimal")
        # Plan with no phase structure at all
        store.write_plan("# My Project\n\nJust build the thing. No phases spelled out.")
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)

        runner = ScriptedRunner([])  # Dispatcher won't actually run in this test
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        phase_id = asyncio.run(orchestrator.approve_plan())
        assert phase_id == "P1"
        final_meta = store.read_meta()
        assert len(final_meta.phases) == 1
        assert final_meta.phases[0].id == "P1"


def test_reject_plan_returns_to_interview() -> None:
    """Rejection should go back to interview with user feedback seeded."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_e2e", name="Test")
        store.write_plan(SAMPLE_PLAN)
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)

        runner = ScriptedRunner([])
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        asyncio.run(orchestrator.reject_plan("Please simplify to 2 phases."))

        final_meta = store.read_meta()
        assert final_meta.status == ProjectStatus.INTERVIEW

        # User feedback should be seeded into the interview log so architect sees it
        interview = store.read_interview()
        assert len(interview) >= 1
        last = interview[-1]
        assert last["role"] == "user"
        assert "simplify to 2 phases" in last["content"]


def test_cannot_approve_plan_from_wrong_state() -> None:
    """Approve should only work from AWAIT_APPROVAL."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_e2e", name="Test")
        # Stay in INIT
        runner = ScriptedRunner([])
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="Cannot approve plan"):
            asyncio.run(orchestrator.approve_plan())
