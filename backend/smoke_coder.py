"""Real end-to-end smoke test: Coder runs a trivial task against the real API.

Catches the class of bug where scripted tests pass but real API calls fail (e.g.,
message construction sending empty content). Trivial task keeps cost and time low.

    ANTHROPIC_API_KEY=sk-ant-... python smoke_coder.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.api_runner import APIRunner  # noqa: E402
from app.agents.coder import Coder  # noqa: E402
from app.orchestrator.task_runner import TaskContext, TaskOutcomeKind  # noqa: E402
from app.sandbox import ProcessSandboxExecutor  # noqa: E402
from app.state import ProjectStore  # noqa: E402


async def run_smoke_test(project_dir: Path) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("FAIL: ANTHROPIC_API_KEY not set")
        return 1

    store = ProjectStore(str(project_dir))
    store.init(project_id="smoke_coder", name="Coder Smoke")
    store.write_plan("# Smoke\n\nTrivial test.\n")

    task = {
        "id": "P1-T1",
        "phase": "P1",
        "title": "Write hello.txt",
        "description": "Create a file named hello.txt in the project root containing the single word 'hello'. That's it.",
        "acceptance_criteria": [
            "A file named hello.txt exists at the project root",
            "The file contains the word 'hello'",
        ],
        "dependencies": [],
        "status": "pending",
        "assigned_to": "coder",
        "iterations": 1,
        "budget_tokens": 20_000,
        "notes": [],
    }
    store.write_tasks([task])

    sandbox = ProcessSandboxExecutor(str(project_dir))
    ctx = TaskContext(
        task=task,
        store=store,
        sandbox=sandbox,
        project_token_budget_remaining=200_000,
        task_token_budget=task["budget_tokens"],
    )

    runner = APIRunner(api_key=api_key)
    coder = Coder(runner=runner, model="claude-sonnet-4-6", wall_clock_seconds=180.0)

    print("→ Running Coder (real API)...")
    start = time.monotonic()
    outcome = None
    tool_calls: list[str] = []
    errors: list[str] = []

    async for event in coder.run(ctx):
        elapsed = time.monotonic() - start
        if event.kind == "tool_use_start":
            name = event.payload.get("name")
            tool_calls.append(name)
            print(f"  [{elapsed:5.1f}s] tool_use: {name}")
        elif event.kind == "tool_result":
            name = event.payload.get("name")
            is_error = event.payload.get("is_error")
            marker = "ERR" if is_error else "ok "
            preview = str(event.payload.get("content_preview", ""))[:80]
            print(f"  [{elapsed:5.1f}s]   → {marker} {preview}")
        elif event.kind == "error":
            msg = event.payload.get("message", "")
            errors.append(msg)
            print(f"  [{elapsed:5.1f}s] ERROR: {msg}")
        elif event.kind == "task_outcome":
            outcome = event.payload.get("outcome")
            print(f"  [{elapsed:5.1f}s] outcome: {outcome.kind.value if outcome else 'None'}")

    elapsed = time.monotonic() - start
    hello_path = project_dir / "hello.txt"
    file_exists = hello_path.exists()
    file_contents = hello_path.read_text().strip() if file_exists else None

    print()
    print(f"Total: {elapsed:.1f}s")
    print(f"Tool calls: {tool_calls}")
    print(f"Outcome: {outcome.kind.value if outcome else 'None'}")
    print(f"hello.txt exists: {file_exists}")
    print(f"hello.txt contents: {file_contents!r}")

    failures = []
    if errors:
        failures.append(f"Got {len(errors)} errors: {errors[0]}")
    if not outcome:
        failures.append("Coder never produced an outcome")
    elif outcome.kind != TaskOutcomeKind.APPROVED:
        failures.append(f"Expected APPROVED outcome, got {outcome.kind.value}")
    if not file_exists:
        failures.append("Coder didn't create hello.txt")
    elif "hello" not in (file_contents or ""):
        failures.append(f"hello.txt contents wrong: {file_contents!r}")

    print()
    if failures:
        print("❌ SMOKE TEST FAILED")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"✓ SMOKE TEST PASSED in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        rc = asyncio.run(run_smoke_test(Path(tmp)))
    sys.exit(rc)
