"""Integration test for AgentEventBuffer routing across all four agent roles.

The user reported "all agent dialogue is showing up under Coder." Code review of
the routing chain (claude_code_runner → execution_loop → orchestrator → buffer)
showed correct attribution at every stage. This test exercises the full chain
end-to-end with a scripted runner so we can either reproduce the bug or rule
it out as a frontend display issue.

Coverage:
  - Architect events (stream_architect_turn) → bucket "architect"
  - Dispatcher events (stream_dispatcher_turn) → bucket "dispatcher"
  - Orchestrator events (task_start, phase_complete, etc.) → bucket "orchestrator"
  - Coder events (Coder agent inside execution loop) → bucket "coder"
  - Reviewer events (Reviewer agent inside execution loop) → bucket "reviewer"

If routing is broken, this test will fail with a precise pointer to which agent
has wrong events. If routing is correct, the user's bug is in the frontend.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any, AsyncIterator

import pytest

from app.agents.base import (
    AgentRunResult,
    Message,
    StreamEvent,
    ToolSpec,
)
from app.orchestrator import Orchestrator
from app.orchestrator.agent_event_buffer import AgentEventBuffer, reset_buffer
from app.state import ProjectStatus, ProjectStore


SAMPLE_PLAN = """# Routing Test — Plan

## Project Summary
Test project for verifying agent event routing.

## MVP Scope
Just enough to drive the orchestrator through every agent.

## P1 — Foundation
- AC: a single trivial task runs end to end
"""


class ScriptedRunner:
    """Minimal scripted runner that emits events with the agent's expected shape."""

    def __init__(
        self,
        script: list[tuple[str, dict[str, Any]]],
        final_text: str = "Done.",
    ) -> None:
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

        for tool_name, tool_input in self._script:
            if tool_name not in tool_map:
                # Skip unknown tools rather than fail — keeps the test resilient
                # to script/tool drift.
                continue

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

            if result.is_error:
                break

        yield StreamEvent(
            kind="text_delta",
            payload={"text": self._final_text},
        )

        yield StreamEvent(
            kind="turn_complete",
            payload={
                "result": AgentRunResult(
                    final_text=self._final_text,
                    messages=[],
                    tokens_input=100,
                    tokens_output=50,
                    stop_reason="end_turn",
                    tool_calls_made=tool_calls,
                )
            },
        )


def test_dispatcher_events_route_to_dispatcher_bucket() -> None:
    """Smallest possible test: run the Dispatcher and check that its events
    landed in the 'dispatcher' bucket of the AgentEventBuffer, not 'coder'
    or anything else.

    This is the test that would catch the reported bug for the Dispatcher
    specifically. If it passes, routing for that agent is correct.
    """
    reset_buffer()  # Fresh buffer for this test
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_route", name="Routing test")
        store.write_plan(SAMPLE_PLAN)

        # Put project in AWAIT_APPROVAL → approve → DISPATCHING
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)

        script = [
            ("read_phase", {}),
            (
                "write_tasks",
                {
                    "tasks": [
                        {
                            "id": "P1-T1",
                            "phase": "P1",
                            "title": "Trivial task",
                            "description": "Does nothing.",
                            "acceptance_criteria": ["nothing"],
                            "dependencies": [],
                        }
                    ]
                },
            ),
            ("mark_dispatch_complete", {"summary": "P1 decomposed"}),
        ]
        runner = ScriptedRunner(script, final_text="P1 decomposed.")
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        # Approve plan to enter DISPATCHING
        asyncio.run(orchestrator.approve_plan())

        async def consume() -> list[StreamEvent]:
            evs: list[StreamEvent] = []
            async for ev in orchestrator.stream_dispatcher_turn():
                evs.append(ev)
            return evs

        events = asyncio.run(consume())
        assert len(events) > 0

        # Now check the buffer. Dispatcher events should be in the dispatcher
        # bucket. NOT in the coder bucket.
        from app.orchestrator.agent_event_buffer import get_buffer

        buf = get_buffer()
        dispatcher_events = buf.fetch("proj_route", agent="dispatcher")
        coder_events = buf.fetch("proj_route", agent="coder")

        # Dispatcher should have events from the run
        assert len(dispatcher_events) > 0, (
            f"Dispatcher bucket is empty after the dispatcher ran. "
            f"This is the routing bug the user reported. "
            f"Got events in other buckets: "
            f"architect={len(buf.fetch('proj_route', agent='architect'))}, "
            f"coder={len(coder_events)}, "
            f"reviewer={len(buf.fetch('proj_route', agent='reviewer'))}, "
            f"orchestrator={len(buf.fetch('proj_route', agent='orchestrator'))}"
        )

        # And critically, the dispatcher's tool calls should NOT have leaked
        # into the coder bucket.
        coder_tool_calls = [
            e for e in coder_events if e["kind"] == "tool_use_start"
        ]
        assert len(coder_tool_calls) == 0, (
            f"Found {len(coder_tool_calls)} dispatcher tool_use_start events "
            f"that landed in the CODER bucket — routing is broken. "
            f"Tool names that got mis-routed: "
            f"{[e['payload'].get('name') for e in coder_tool_calls]}"
        )


def test_architect_events_route_to_architect_bucket() -> None:
    """Same shape, for the Architect."""
    reset_buffer()
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_arch_route", name="Arch routing test")

        # Architect runs in INTERVIEW state
        meta = store.read_meta()
        meta.status = ProjectStatus.INTERVIEW
        store.write_meta(meta)

        # Architect doesn't actually need to call tools to test routing —
        # an empty script with just text_delta is fine. The orchestrator
        # records the event regardless of kind.
        runner = ScriptedRunner(script=[], final_text="Tell me more.")
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        async def consume() -> list[StreamEvent]:
            evs: list[StreamEvent] = []
            async for ev in orchestrator.stream_architect_turn(user_message="hi"):
                evs.append(ev)
            return evs

        events = asyncio.run(consume())
        assert len(events) > 0

        from app.orchestrator.agent_event_buffer import get_buffer

        buf = get_buffer()
        architect_events = buf.fetch("proj_arch_route", agent="architect")
        coder_events = buf.fetch("proj_arch_route", agent="coder")

        assert len(architect_events) > 0, (
            "Architect bucket is empty after Architect ran. Routing broken."
        )
        assert len(coder_events) == 0, (
            f"Found {len(coder_events)} Architect events that leaked into the "
            f"CODER bucket — routing is broken."
        )


def test_reviewer_events_via_full_execution_loop_route_to_reviewer_bucket() -> None:
    """The most direct test: drive the full execution loop including the
    Reviewer invocation and verify Reviewer events land in the Reviewer
    bucket of the AgentEventBuffer.

    This is the path that the user's bug report describes. Previous tests
    only exercised the Reviewer in isolation; this one runs it through the
    execution_loop where _agent tagging happens.
    """
    from app.agents.base import AgentRunResult
    from app.orchestrator.execution_loop import ExecutionLoop
    from app.agents.reviewer import Reviewer

    reset_buffer()
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_rev_route", name="Reviewer routing test")

        # Skip ahead to EXECUTING with one task ready for the Coder
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        meta.current_phase = "P1"
        from app.state import ProjectPhase

        meta.phases = [ProjectPhase(id="P1", title="Phase 1", status="active")]
        store.write_meta(meta)
        store.write_tasks(
            [
                {
                    "id": "P1-T1",
                    "phase": "P1",
                    "title": "Trivial task",
                    "description": "Does nothing",
                    "acceptance_criteria": ["nothing"],
                    "dependencies": [],
                    "status": "pending",
                    "iterations": 0,
                }
            ]
        )

        # A "runner" that, given a role, plays the right script
        class RoleAwareRunner:
            async def run(self, **kwargs: Any) -> AgentRunResult:
                raise NotImplementedError

            async def stream(
                self, **kwargs: Any
            ) -> AsyncIterator[StreamEvent]:
                role = kwargs["role"]
                tool_map = {t.name: t.executor for t in kwargs["tools"]}

                if role == "coder":
                    # Coder reads the task, then signals approved
                    yield StreamEvent(
                        kind="text_delta", payload={"text": "CODER_TEXT"}
                    )
                    if "read_task" in tool_map:
                        yield StreamEvent(
                            kind="tool_use_start",
                            payload={"name": "read_task", "input": {}},
                        )
                        result = await tool_map["read_task"]({})
                        yield StreamEvent(
                            kind="tool_result",
                            payload={
                                "name": "read_task",
                                "is_error": result.is_error,
                                "content_preview": str(result.content)[:200],
                            },
                        )
                    if "signal_outcome" in tool_map:
                        yield StreamEvent(
                            kind="tool_use_start",
                            payload={
                                "name": "signal_outcome",
                                "input": {"status": "approved", "summary": "Done"},
                            },
                        )
                        result = await tool_map["signal_outcome"](
                            {"status": "approved", "summary": "Done"}
                        )
                        yield StreamEvent(
                            kind="tool_result",
                            payload={
                                "name": "signal_outcome",
                                "is_error": result.is_error,
                                "content_preview": str(result.content)[:200],
                            },
                        )
                    yield StreamEvent(
                        kind="turn_complete",
                        payload={
                            "result": AgentRunResult(
                                final_text="Done",
                                messages=[],
                                tokens_input=100,
                                tokens_output=50,
                                stop_reason="end_turn",
                                tool_calls_made=2,
                            )
                        },
                    )
                elif role == "reviewer":
                    # Reviewer emits identifying text then approves via submit_review
                    yield StreamEvent(
                        kind="text_delta",
                        payload={"text": "REVIEWER_TEXT_UNIQUE"},
                    )
                    if "submit_review" in tool_map:
                        yield StreamEvent(
                            kind="tool_use_start",
                            payload={
                                "name": "submit_review",
                                "input": {"outcome": "approve", "summary": "LGTM"},
                            },
                        )
                        result = await tool_map["submit_review"](
                            {"outcome": "approve", "summary": "LGTM"}
                        )
                        yield StreamEvent(
                            kind="tool_result",
                            payload={
                                "name": "submit_review",
                                "is_error": result.is_error,
                                "content_preview": str(result.content)[:200],
                            },
                        )
                    yield StreamEvent(
                        kind="turn_complete",
                        payload={
                            "result": AgentRunResult(
                                final_text="LGTM",
                                messages=[],
                                tokens_input=200,
                                tokens_output=100,
                                stop_reason="end_turn",
                                tool_calls_made=1,
                            )
                        },
                    )

        runner = RoleAwareRunner()
        orchestrator = Orchestrator(store=store, runner=runner)  # type: ignore[arg-type]

        async def consume() -> list[StreamEvent]:
            evs: list[StreamEvent] = []
            async for ev in orchestrator.stream_execution_loop():
                evs.append(ev)
                # Safety: don't run forever if loop misbehaves
                if len(evs) > 200:
                    break
            return evs

        events = asyncio.run(consume())
        assert len(events) > 0

        from app.orchestrator.agent_event_buffer import get_buffer

        buf = get_buffer()
        coder_events = buf.fetch("proj_rev_route", agent="coder")
        reviewer_events = buf.fetch("proj_rev_route", agent="reviewer")

        # Coder produced CODER_TEXT; Reviewer produced REVIEWER_TEXT_UNIQUE.
        # Each must land in its own bucket.
        coder_text = " ".join(
            str(e["payload"].get("text", ""))
            for e in coder_events
            if e["kind"] == "text_delta"
        )
        reviewer_text = " ".join(
            str(e["payload"].get("text", ""))
            for e in reviewer_events
            if e["kind"] == "text_delta"
        )

        assert "CODER_TEXT" in coder_text, (
            f"Coder bucket missing Coder's own text. Got: {coder_text!r}. "
            f"Coder events: {len(coder_events)}, Reviewer events: {len(reviewer_events)}"
        )
        assert "REVIEWER_TEXT_UNIQUE" in reviewer_text, (
            f"Reviewer bucket missing Reviewer's own text. "
            f"Got Reviewer bucket text: {reviewer_text!r}. "
            f"This is the routing bug — Reviewer events landed in another bucket. "
            f"Coder events ({len(coder_events)}): "
            f"{[(e['kind'], str(e['payload'])[:80]) for e in coder_events[-5:]]}"
        )

        # And the cross-contamination assertion — Reviewer's text should NEVER
        # appear in the Coder bucket
        assert "REVIEWER_TEXT_UNIQUE" not in coder_text, (
            f"Reviewer's text leaked into the CODER bucket. "
            f"This is exactly the user-reported bug. "
            f"Coder bucket text: {coder_text!r}"
        )
