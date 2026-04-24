"""Tests for the Coder agent.

Uses a scripted APIRunner (not the real Anthropic API) to exercise the Coder's
self-enforcement logic: budget cutoff, timeout, outcome signaling, and graceful
fallback when the model forgets to signal.

We're not testing the model's code-writing ability — that requires real API calls
and is qualitative. We're testing that the wrapper around the model (token tracking,
timeout wrapper, outcome-signal short-circuit, fallback outcomes) behaves correctly
under each failure mode.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from app.agents.base import (
    AgentRunner,
    AgentRunResult,
    Message,
    StreamEvent,
    ToolSpec,
)
from app.agents.coder import Coder
from app.orchestrator.task_runner import TaskContext, TaskOutcome, TaskOutcomeKind
from app.sandbox import ProcessSandboxExecutor
from app.state import ProjectStatus, ProjectStore


# ---- Test helpers ------------------------------------------------------------------


def _make_task(task_id: str = "P1-T1", **overrides) -> dict[str, Any]:
    base = {
        "id": task_id,
        "phase": "P1",
        "title": "Test task",
        "description": "Write a hello world file",
        "acceptance_criteria": ["file exists", "says hello"],
        "dependencies": [],
        "status": "in_progress",
        "assigned_to": "coder",
        "iterations": 1,
        "budget_tokens": 50_000,
        "notes": [],
    }
    base.update(overrides)
    return base


def _make_ctx(tmp: str, task: dict[str, Any]) -> TaskContext:
    store = ProjectStore(tmp)
    store.init(project_id="proj_test", name="Test")
    store.write_tasks([task])
    sandbox = ProcessSandboxExecutor(tmp)
    return TaskContext(
        task=task,
        store=store,
        sandbox=sandbox,
        project_token_budget_remaining=200_000,
        task_token_budget=task["budget_tokens"],
    )


class ScriptedAPIRunner:
    """Fake APIRunner that plays a script of (tool_name, tool_input) calls, then emits usage + turn_complete.

    The coder_tools that map to tool_name must be invocable via `tool.executor(input)`.
    We actually call them so side effects (file writes, outcome signals) happen.
    """

    def __init__(
        self,
        script: list[tuple[str, dict[str, Any]]],
        *,
        per_call_input_tokens: int = 500,
        per_call_output_tokens: int = 200,
    ) -> None:
        self._script = script
        self._in = per_call_input_tokens
        self._out = per_call_output_tokens

    async def run(self, **_kwargs) -> AgentRunResult:
        raise NotImplementedError("ScriptedAPIRunner only supports stream()")

    async def stream(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int = 4096,
        max_iterations: int = 40,
    ) -> AsyncIterator[StreamEvent]:
        tool_map = {t.name: t for t in tools}
        tool_calls_made = 0
        total_in = 0
        total_out = 0

        for tool_name, tool_input in self._script:
            if tool_name not in tool_map:
                yield StreamEvent(
                    kind="error",
                    payload={"message": f"Scripted tool not found: {tool_name}"},
                )
                return

            yield StreamEvent(
                kind="tool_use_start",
                payload={"name": tool_name, "input": tool_input},
            )
            result = await tool_map[tool_name].executor(tool_input)
            tool_calls_made += 1
            yield StreamEvent(
                kind="tool_result",
                payload={
                    "name": tool_name,
                    "is_error": result.is_error,
                    "content_preview": str(result.content)[:200],
                    # Full content included in tests so assertions can inspect the
                    # whole tool output. Production code path in api_runner only
                    # forwards content_preview to the WebSocket.
                    "content": str(result.content),
                },
            )

            # Emit a usage tick per tool call so budget-tracking tests can trigger
            total_in += self._in
            total_out += self._out
            yield StreamEvent(
                kind="usage",
                payload={"input_tokens": self._in, "output_tokens": self._out},
            )

        yield StreamEvent(
            kind="turn_complete",
            payload={
                "result": AgentRunResult(
                    final_text="done",
                    messages=[],
                    tokens_input=total_in,
                    tokens_output=total_out,
                    stop_reason="end_turn",
                    tool_calls_made=tool_calls_made,
                )
            },
        )


async def _drain(gen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in gen]


# ---- Tests -------------------------------------------------------------------------


def test_coder_emits_approved_outcome_when_signal_called_with_approved() -> None:
    """Happy path: Coder explores, writes a file, runs a test, calls signal_outcome(approved)."""
    with tempfile.TemporaryDirectory() as tmp:
        task = _make_task()
        ctx = _make_ctx(tmp, task)

        script = [
            ("read_task", {}),
            (
                "fs_write",
                {"path": "hello.txt", "content": "hello"},
            ),
            (
                "bash",
                {"argv": ["cat", "hello.txt"]},
            ),
            (
                "signal_outcome",
                {"status": "approved", "summary": "Wrote hello.txt containing 'hello'."},
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)

        events = asyncio.run(_drain(coder.run(ctx)))

        # Must emit exactly one task_outcome event as the final or near-final event
        outcome_events = [e for e in events if e.kind == "task_outcome"]
        assert len(outcome_events) == 1
        outcome = outcome_events[0].payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.APPROVED
        assert "hello" in outcome.summary

        # File was actually written via fs_write
        assert (Path(tmp) / "hello.txt").read_text() == "hello"


def test_coder_emits_blocked_outcome_when_signal_blocked_called() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            ("read_task", {}),
            (
                "signal_outcome",
                {
                    "status": "blocked",
                    "block_reason": "Task spec requires a library that isn't available.",
                },
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)

        events = asyncio.run(_drain(coder.run(ctx)))
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.BLOCKED
        assert "library" in outcome.block_reason.lower()


def test_coder_emits_needs_rework_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            (
                "signal_outcome",
                {
                    "status": "needs_rework",
                    "rework_notes": "I didn't handle the empty input case",
                },
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.NEEDS_REWORK
        assert "empty input" in outcome.rework_notes


def test_coder_synthesizes_failed_outcome_when_no_signal_called() -> None:
    """If the model ends its turn without calling signal_outcome, we must emit FAILED —
    not silently approve, not hang. This is the critical safety property."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        # Script makes some tool calls but never calls signal_outcome
        script = [
            ("read_task", {}),
            ("fs_list", {"path": "."}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)

        events = asyncio.run(_drain(coder.run(ctx)))
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.FAILED
        assert "signal_outcome" in outcome.failure_reason


def test_coder_emits_blocked_when_budget_exhausted_without_signal() -> None:
    """Coder running over budget without calling signal_outcome emits BLOCKED (not FAILED).

    Semantically different: FAILED means the Coder didn't try; BLOCKED means it tried
    but ran out of budget — a recoverable situation the user can address by raising
    the per-task budget."""
    with tempfile.TemporaryDirectory() as tmp:
        # Tiny budget; each scripted tool call costs 500+200=700 tokens
        task = _make_task(budget_tokens=2_000)
        ctx = _make_ctx(tmp, task)

        # Enough tool calls to blow past the 90% cutoff (1800 tokens)
        script = [
            ("read_task", {}),
            ("fs_list", {"path": "."}),
            ("fs_list", {"path": "."}),
            ("fs_list", {"path": "."}),
            ("fs_list", {"path": "."}),
            # No signal_outcome call
        ]
        runner = ScriptedAPIRunner(script, per_call_input_tokens=500, per_call_output_tokens=200)
        coder = Coder(runner=runner, wall_clock_seconds=30)

        events = asyncio.run(_drain(coder.run(ctx)))
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.BLOCKED
        assert "budget" in outcome.block_reason.lower()


def test_coder_honors_wall_clock_timeout() -> None:
    """If the inner stream hangs forever, the Coder must return FAILED on timeout rather
    than leaving the execution loop stuck."""

    class HangingRunner:
        async def run(self, **_kw):
            raise NotImplementedError

        async def stream(self, **_kw):
            # Yield one event, then sleep longer than the timeout allows
            yield StreamEvent(kind="tool_use_start", payload={"name": "x", "input": {}})
            await asyncio.sleep(10)
            yield StreamEvent(kind="turn_complete", payload={})

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        coder = Coder(runner=HangingRunner(), wall_clock_seconds=1)
        events = asyncio.run(_drain(coder.run(ctx)))

        # Should get a task_outcome with FAILED even though the runner hung
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.FAILED
        assert "wall clock" in outcome.failure_reason.lower() or "wall-clock" in outcome.failure_reason.lower()


def test_coder_tools_log_file_writes_to_decisions() -> None:
    """The user needs visibility into what files the Coder wrote. Each fs_write must
    show up in decisions.log."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            ("fs_write", {"path": "src/a.py", "content": "x = 1\n"}),
            ("fs_write", {"path": "src/b.py", "content": "y = 2\n"}),
            ("signal_outcome", {"status": "approved", "summary": "Wrote two files"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        asyncio.run(_drain(coder.run(ctx)))

        decisions = ctx.store.read_decisions()
        write_decisions = [d for d in decisions if d.get("kind") == "file_written"]
        assert len(write_decisions) == 2
        paths = sorted(d["path"] for d in write_decisions)
        assert paths == ["src/a.py", "src/b.py"]


def test_coder_tools_log_bash_calls_to_decisions() -> None:
    """Bash calls with exit codes and durations must be in decisions.log for audit."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            ("bash", {"argv": ["echo", "hello"]}),
            ("signal_outcome", {"status": "approved", "summary": "did echo"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        asyncio.run(_drain(coder.run(ctx)))

        decisions = ctx.store.read_decisions()
        bash_decisions = [d for d in decisions if d.get("kind") == "bash"]
        assert len(bash_decisions) == 1
        assert bash_decisions[0]["argv"] == ["echo", "hello"]
        assert bash_decisions[0]["exit_code"] == 0


def test_coder_signal_outcome_requires_summary_for_approved() -> None:
    """If the model calls signal_outcome(approved) without summary, we must reject the
    tool call — otherwise the user gets approvals with no explanation."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            ("signal_outcome", {"status": "approved"}),  # missing summary
            # After the error, a follow-up call with a summary succeeds
            ("signal_outcome", {"status": "approved", "summary": "ok"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        # First tool call must have been an error, second must have succeeded
        tool_results = [e for e in events if e.kind == "tool_result" and e.payload.get("name") == "signal_outcome"]
        assert tool_results[0].payload["is_error"] is True
        # Final outcome is APPROVED via the second call
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.APPROVED


def test_coder_fs_write_rejects_path_outside_project() -> None:
    """Agent-generated paths outside the project root must be rejected at the tool level,
    not bypass the sandbox. This is a sandbox-integration regression guard."""
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        ctx = _make_ctx(tmp_a, _make_task())
        # Try to write into tmp_b, which is outside the project root
        outside_path = str(Path(tmp_b) / "leaked.txt")
        script = [
            ("fs_write", {"path": outside_path, "content": "leaked"}),
            ("signal_outcome", {"status": "blocked", "block_reason": "Got an error"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        asyncio.run(_drain(coder.run(ctx)))

        # File must not have been written outside
        assert not (Path(tmp_b) / "leaked.txt").exists()


def test_coder_emits_needs_user_review_outcome_with_checklist_and_run_command() -> None:
    """Happy path for the UI/visual verification handoff."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            (
                "signal_outcome",
                {
                    "status": "needs_user_review",
                    "summary": "Built the canvas and loop",
                    "review_checklist": [
                        "Open index.html in Chrome",
                        "Confirm 960x540 canvas renders",
                    ],
                    "review_run_command": "python -m http.server 8000",
                    "review_files_to_check": ["index.html"],
                },
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.NEEDS_USER_REVIEW
        assert outcome.summary == "Built the canvas and loop"
        assert len(outcome.review_checklist) == 2
        assert outcome.review_run_command == "python -m http.server 8000"
        assert outcome.review_files_to_check == ["index.html"]


def test_coder_signal_outcome_needs_user_review_requires_checklist() -> None:
    """Empty or missing checklist must be rejected — the whole point of this outcome
    is to give the user a concrete set of steps to verify."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            (
                "signal_outcome",
                {
                    "status": "needs_user_review",
                    "summary": "Built something",
                    # Missing checklist
                    "review_run_command": "python -m http.server 8000",
                },
            ),
            # Recovery: call again with checklist
            (
                "signal_outcome",
                {
                    "status": "needs_user_review",
                    "summary": "Built something",
                    "review_checklist": ["Check it looks right"],
                    "review_run_command": "python -m http.server 8000",
                },
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        # First signal_outcome must be rejected with is_error
        tool_results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "signal_outcome"
        ]
        assert tool_results[0].payload["is_error"] is True
        assert "checklist" in str(tool_results[0].payload["content_preview"]).lower()

        # Second call succeeds and produces the outcome
        outcome = next(e for e in events if e.kind == "task_outcome").payload["outcome"]
        assert outcome.kind == TaskOutcomeKind.NEEDS_USER_REVIEW


def test_coder_signal_outcome_needs_user_review_requires_run_command() -> None:
    """Missing run_command must be rejected — Option 3 says the Coder gives the user
    the exact command to run, so they don't have to figure it out themselves."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        script = [
            (
                "signal_outcome",
                {
                    "status": "needs_user_review",
                    "summary": "Built something",
                    "review_checklist": ["Check it"],
                    # Missing review_run_command
                },
            ),
            (
                "signal_outcome",
                {
                    "status": "blocked",
                    "block_reason": "Gave up",
                },
            ),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        tool_results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "signal_outcome"
        ]
        assert tool_results[0].payload["is_error"] is True
        preview = str(tool_results[0].payload["content_preview"]).lower()
        assert "run_command" in preview or "run/view" in preview


# ---- Decision history surfacing ----------------------------------------------------


def test_coder_read_task_does_not_include_history_on_first_iteration() -> None:
    """Iteration 1 has no prior attempts — including empty history would waste tokens
    and possibly confuse the model. read_task should NOT surface relevant_history."""
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task(iterations=1))
        # Even if there are decisions in the log, iteration 1 skips history.
        asyncio.run(
            ctx.store.append_decision(
                {"actor": "orchestrator", "kind": "task_rework", "task_id": "P1-T1",
                 "notes": "some old thing"}
            )
        )
        read_task_result = {"content": ""}

        async def intercept(tool_name: str, tool_input: dict[str, Any]) -> None:
            pass  # not needed; we'll capture via the script

        script = [
            ("read_task", {}),
            ("signal_outcome", {"status": "approved", "summary": "ok"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        # Find the read_task tool_result and inspect its content
        read_task_results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "read_task"
        ]
        assert len(read_task_results) == 1
        content = read_task_results[0].payload["content"]
        # relevant_history key should not appear in iteration 1 output
        assert "relevant_history" not in content


def test_coder_read_task_includes_relevant_history_on_retry() -> None:
    """On iteration 2+, the Coder's read_task must include curated history for THIS task
    so the model sees what was tried before without having to call extra tools."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task(iterations=2))
        # Seed the decisions log with realistic prior-iteration history for this task
        asyncio.run(ctx.store.append_decision({
            "actor": "orchestrator",
            "kind": "task_rework",
            "task_id": "P1-T1",
            "notes": "Tests failed — handle empty list case",
        }))
        asyncio.run(ctx.store.append_decision({
            "actor": "coder",
            "kind": "bash",
            "task_id": "P1-T1",
            "argv": ["pytest", "-q"],
            "exit_code": 1,  # failed — should be auto-included
            "timed_out": False,
            "duration_ms": 200,
        }))
        asyncio.run(ctx.store.append_decision({
            "actor": "coder",
            "kind": "bash",
            "task_id": "P1-T1",
            "argv": ["ls"],
            "exit_code": 0,  # succeeded — should NOT be auto-included
            "timed_out": False,
            "duration_ms": 5,
        }))
        # A decision from a *different* task — must be filtered out
        asyncio.run(ctx.store.append_decision({
            "actor": "orchestrator",
            "kind": "task_rework",
            "task_id": "P2-T9",
            "notes": "Different task; should not leak into P1-T1's history",
        }))

        script = [
            ("read_task", {}),
            ("signal_outcome", {"status": "approved", "summary": "ok"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        read_task_results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "read_task"
        ]
        content = read_task_results[0].payload["content"]

        # Auto-included rework note from this task
        assert "relevant_history" in content
        assert "empty list case" in content
        # Failed bash included
        assert "pytest" in content
        # Successful `ls` bash must NOT be included (would be noise on retry)
        assert '"argv"' in content  # the failed pytest has argv
        # The specific successful ls entry's argv '["ls"]' shouldn't appear
        # (because we exclude successful bash from auto-include).
        assert '["ls"]' not in content

        # Decision from P2-T9 must not be in P1-T1's history
        assert "P2-T9" not in content
        assert "should not leak" not in content


def test_coder_read_decisions_tool_filters_by_kind() -> None:
    """The explicit read_decisions tool should filter by kind when requested."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        asyncio.run(ctx.store.append_decision({
            "actor": "coder", "kind": "bash", "task_id": "P1-T1",
            "argv": ["pytest"], "exit_code": 0, "timed_out": False, "duration_ms": 100,
        }))
        asyncio.run(ctx.store.append_decision({
            "actor": "coder", "kind": "file_written", "task_id": "P1-T1",
            "path": "foo.py", "bytes": 100,
        }))
        asyncio.run(ctx.store.append_decision({
            "actor": "coder", "kind": "note", "task_id": "P1-T1",
            "note": "Chose X over Y because …",
        }))

        # Fetch only bash calls for this task
        script = [
            ("read_decisions", {"kinds": ["bash"], "limit": 10}),
            ("signal_outcome", {"status": "approved", "summary": "ok"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "read_decisions"
        ]
        content = results[0].payload["content"]
        # Should contain the bash entry
        assert "pytest" in content
        # Should NOT contain the file_written or note entries (kind filter excluded them)
        assert "foo.py" not in content
        assert "Chose X over Y" not in content


def test_coder_read_decisions_tool_scope_all_reaches_other_tasks() -> None:
    """With scope='all' the Coder can see cross-task history, e.g. for consistency."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(tmp, _make_task())
        asyncio.run(ctx.store.append_decision({
            "actor": "orchestrator", "kind": "task_approved", "task_id": "P1-T0",
            "summary": "Built the HTML shell using Tailwind utility classes",
        }))

        script = [
            ("read_decisions", {"scope": "all", "kinds": ["task_approved"]}),
            ("signal_outcome", {"status": "approved", "summary": "ok"}),
        ]
        runner = ScriptedAPIRunner(script)
        coder = Coder(runner=runner, wall_clock_seconds=30)
        events = asyncio.run(_drain(coder.run(ctx)))

        results = [
            e for e in events
            if e.kind == "tool_result" and e.payload.get("name") == "read_decisions"
        ]
        content = results[0].payload["content"]
        # The P1-T0 approval from another task should be visible here
        assert "P1-T0" in content or "Tailwind" in content
