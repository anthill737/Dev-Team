"""Tests for reviewer_tools.write_verification_script.

The scratch-write tool is the Reviewer's only mutation capability — it must:
  - Only write under .devteam/review-scratch/<task_id>/
  - Reject path traversal (slashes, leading dot, subdirs)
  - Reject missing filename / missing content
  - Actually write the file

Also tested: Execution loop cleans the scratch dir when a task is marked done.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.sandbox import ProcessSandboxExecutor
from app.state import ProjectStore
from app.tools.reviewer_tools import ReviewSignal, build_reviewer_tools


def _task(task_id: str = "P1-T1") -> dict:
    return {
        "id": task_id,
        "phase": "P1",
        "title": "test task",
        "description": "x",
        "acceptance_criteria": ["works"],
        "iterations": 1,
        "review_cycles": 0,
    }


def _get_write_tool(store, sandbox, task):
    """Find the write_verification_script executor from the built tool list."""
    received: list[ReviewSignal] = []
    tools = build_reviewer_tools(
        store=store,
        sandbox=sandbox,
        task=task,
        signal_receiver=lambda s: received.append(s),
    )
    for t in tools:
        if t.name == "write_verification_script":
            return t.executor
    raise AssertionError("write_verification_script tool not registered")


def test_write_verification_script_writes_under_scratch_dir() -> None:
    """Happy path: tool writes script to .devteam/review-scratch/<task_id>/ and
    returns a path + usage hint."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p", name="t")
        sandbox = ProcessSandboxExecutor(tmp)
        task = _task()
        writer = _get_write_tool(store, sandbox, task)

        result = asyncio.run(
            writer({"filename": "verify.py", "content": "print('hi')\n"})
        )
        assert not result.is_error
        # File must actually exist on disk at the expected path
        expected = Path(tmp) / ".devteam" / "review-scratch" / "P1-T1" / "verify.py"
        assert expected.exists()
        assert expected.read_text() == "print('hi')\n"
        # Response should mention how to run it
        assert "python" in result.content.lower()
        assert "verify.py" in result.content


def test_write_verification_script_rejects_path_traversal() -> None:
    """No slashes, no backslashes, no leading dot — Reviewer must not escape
    scratch dir to modify Coder's code."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p", name="t")
        sandbox = ProcessSandboxExecutor(tmp)
        writer = _get_write_tool(store, sandbox, _task())

        bad_names = [
            "../evil.py",
            "../../etc/passwd",
            "subdir/script.py",
            "sub\\script.py",
            ".hidden.py",
        ]
        for name in bad_names:
            result = asyncio.run(
                writer({"filename": name, "content": "whatever"})
            )
            assert result.is_error, f"Expected rejection for filename: {name!r}"
            assert "no slashes" in result.content.lower() or "no leading dot" in result.content.lower()


def test_write_verification_script_rejects_missing_args() -> None:
    """Both filename and content are required."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p", name="t")
        sandbox = ProcessSandboxExecutor(tmp)
        writer = _get_write_tool(store, sandbox, _task())

        r1 = asyncio.run(writer({"content": "x"}))
        assert r1.is_error
        assert "filename" in r1.content.lower()

        r2 = asyncio.run(writer({"filename": "ok.py"}))
        assert r2.is_error
        assert "content" in r2.content.lower()


def test_cleanup_review_scratch_removes_dir_on_task_done() -> None:
    """Execution loop calls _cleanup_review_scratch when a task is marked done.
    This test exercises the method directly — integration through the full loop
    is covered by existing tests."""
    from app.agents.base import StreamEvent
    from app.orchestrator.execution_loop import ExecutionLoop
    from app.orchestrator.task_runner import TaskContext

    class DummyRunner:
        async def run(self, ctx: TaskContext):
            async def gen():
                if False:
                    yield StreamEvent(kind="noop", payload={})
            return gen()

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p", name="t")
        sandbox = ProcessSandboxExecutor(tmp)

        # Create a scratch directory with a file
        scratch = Path(tmp) / ".devteam" / "review-scratch" / "P1-T99"
        scratch.mkdir(parents=True)
        (scratch / "verify.py").write_text("print('x')\n")
        assert scratch.exists()

        loop = ExecutionLoop(
            store=store, sandbox=sandbox, runner=DummyRunner()  # type: ignore[arg-type]
        )
        loop._cleanup_review_scratch("P1-T99")

        # Directory gone
        assert not scratch.exists()


def test_cleanup_review_scratch_is_safe_when_dir_absent() -> None:
    """If scratch was never created (task didn't need it), cleanup must not
    raise — it's called unconditionally on every task-done."""
    from app.orchestrator.execution_loop import ExecutionLoop

    class DummyRunner:
        async def run(self, ctx):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="p", name="t")
        sandbox = ProcessSandboxExecutor(tmp)
        loop = ExecutionLoop(
            store=store, sandbox=sandbox, runner=DummyRunner()  # type: ignore[arg-type]
        )
        # No scratch dir exists. Should complete silently.
        loop._cleanup_review_scratch("nonexistent-task")
