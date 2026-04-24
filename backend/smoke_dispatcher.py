"""Real end-to-end smoke test: drives the Dispatcher against the actual Anthropic API.

This test is NOT part of the unit test suite. It makes real API calls and costs money
(typically a few cents per run). Run it manually when you've changed prompts, tools,
or anything that affects how the model behaves in production.

    ANTHROPIC_API_KEY=sk-ant-... python smoke_dispatcher.py

Rationale: unit tests with scripted runners can't catch prompt-engineering bugs. The
Dispatcher hang observed in production (reflective-practice block causing verbose output
truncation) passed every unit test because the scripted runner doesn't generate text. A
real smoke test catches these problems in 60 seconds instead of after a user report.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Path so we can import the app
BACKEND_ROOT = Path(__file__).parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.api_runner import APIRunner  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.state import ProjectStatus, ProjectStore  # noqa: E402
from app.state.store import ProjectPhase  # noqa: E402


# A dense single-phase plan approximating the real Stickman Dodgeball project that
# triggered the Dispatcher hang in production. If this plan dispatches successfully
# in under 3 minutes, the fix works. If it hangs, the fix does not.
FIXTURE_PLAN = """# Stickman Dodgeball — Plan

## Project Summary

A browser-based 2D side-view dodgeball game featuring literal stick figures. Player
controls one stickman on a team of 5 against a team of 5 AI enemies in a gym-style
arena split by a center line. Built to run identically in desktop Chrome and mobile
Chrome on Android.

## P1: MVP

Build the full playable MVP. Single phase because there's no meaningful mid-build
approval gate for a solo fun project.

### Tech Stack

- Phaser 3 (game framework, static HTML/JS/CSS)
- Arcade Physics (Phaser built-in, lightweight AABB + gravity)
- Vanilla JavaScript (no React/Vue)
- Vite (dev server + bundling)
- Vitest (unit testing for pure-logic modules)

### MVP Scope

IN:
- Single-player only, vs AI (no multiplayer)
- 5v5: player + 4 AI teammates vs 5 AI enemies
- Side-view 2D arena with center line neither team may cross
- Stick-figure art: black lines on plain light background
- Physics: gravity, ball arcs when thrown, AABB collisions, balls bounce off walls
- 5 balls in play, pickable by walking over
- Throwing with preset arc; AI throws same way
- Elimination: ball-to-body contact removes that stickman; no catching in MVP
- Win = all 5 enemies eliminated → victory screen
- Loss = all 5 of player's team eliminated → defeat screen
- Restart button on both end screens
- Controls: on-screen touch buttons (left/right/jump/throw/pickup) that also work
  with keyboard (Arrow keys or A/D for move, Space for jump, F or Enter for throw)
- Landscape orientation lock on mobile
- Responsive canvas: fits desktop window and mobile landscape
- AI behavior: if no ball → move to nearest ball and pick up; if holding → move to
  throw position and throw at random living enemy; if enemy ball incoming and close →
  attempt dodge (no guarantee)
- Simple start screen with Play button
- Vitest test suite for pure-logic pieces (collision math, elimination rules,
  AI decision-making on mock world state) — AT LEAST 15 tests passing

OUT:
- Catching mechanic
- Online multiplayer
- Local multiplayer (two players on one device)
- Multiple levels/rounds progression
- Sound/music
- Sophisticated AI (pathfinding, state machines beyond simple rules)
- Art beyond stick figures
- Menus beyond start/restart
- Persistence/high scores
- App store deployment

### Success Criteria

1. `npm run dev` serves the game at localhost:5173 and it loads in Chrome
2. Canvas renders at 960x540 and is visible with stickman sprites and center line
3. Arrow keys move the player stickman left/right; Space jumps
4. F or Enter throws a ball if the player is holding one
5. Walking over a ball picks it up (only one ball at a time)
6. AI enemies move, pick up balls, and throw them at the player's team
7. Ball hitting a stickman eliminates them (removed from scene)
8. All 5 enemies eliminated → victory screen with Restart
9. All 5 player-team stickmen eliminated → defeat screen with Restart
10. Vitest suite runs with `npm test` and at least 15 tests pass
11. Touch buttons render and work on mobile-sized viewport
12. Landscape orientation lock message shows on mobile portrait

### Open Questions

- None — scope fully resolved in interview
"""


async def run_smoke_test(project_dir: Path) -> int:
    """Returns 0 on success, nonzero on failure. Prints progress + timings."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("FAIL: ANTHROPIC_API_KEY not set")
        return 1

    # Set up a project already at AWAIT_APPROVAL with a known plan
    store = ProjectStore(str(project_dir))
    store.init(project_id="smoke_test", name="Pomodoro Smoke Test")

    meta = store.read_meta()
    meta.status = ProjectStatus.AWAIT_APPROVAL
    store.write_meta(meta)
    store.write_plan(FIXTURE_PLAN)

    runner = APIRunner(api_key=api_key)
    orchestrator = Orchestrator(store=store, runner=runner)

    # Approve the plan — this parses phases and transitions to DISPATCHING
    print("→ Approving plan...")
    phase_id = await orchestrator.approve_plan()
    print(f"  Plan approved. First phase: {phase_id}")

    meta = store.read_meta()
    assert meta.status == ProjectStatus.DISPATCHING, f"Expected DISPATCHING, got {meta.status}"

    # Drive the Dispatcher
    print("→ Running Dispatcher (this is the real API call)...")
    start = time.monotonic()
    last_event_time = start
    tool_calls = []
    text_tokens_seen = 0

    async for event in orchestrator.stream_dispatcher_turn():
        now = time.monotonic()
        elapsed = now - start

        if event.kind == "tool_use_start":
            name = event.payload.get("name")
            tool_calls.append(name)
            print(f"  [{elapsed:5.1f}s] tool_use: {name}")
            last_event_time = now
        elif event.kind == "tool_result":
            name = event.payload.get("name")
            is_error = event.payload.get("is_error")
            preview = event.payload.get("content_preview", "")[:80]
            marker = "ERR" if is_error else "ok "
            print(f"  [{elapsed:5.1f}s]   → {marker} {preview}")
            last_event_time = now
        elif event.kind == "text_delta":
            text_tokens_seen += 1
            # Print a dot every 100 chunks so we can see verbose-text problems
            if text_tokens_seen % 100 == 0:
                print(f"  [{elapsed:5.1f}s]   ...text chunks: {text_tokens_seen}")
        elif event.kind == "turn_complete":
            result = event.payload.get("result")
            print(f"  [{elapsed:5.1f}s] turn_complete: stop={result.stop_reason}, "
                  f"in={result.tokens_input}, out={result.tokens_output}, "
                  f"tools={result.tool_calls_made}")

        # Bail out if we're clearly hung (nothing useful for 90s)
        if now - last_event_time > 90.0:
            print(f"FAIL: no progress for 90s — aborting")
            break

    elapsed = time.monotonic() - start

    # Check final state
    final_meta = store.read_meta()
    tasks = store.read_tasks()
    print()
    print(f"Total time: {elapsed:.1f}s")
    print(f"Final project status: {final_meta.status.value}")
    print(f"Tool calls made: {tool_calls}")
    print(f"Text chunks streamed: {text_tokens_seen}")
    print(f"Tasks written: {len(tasks)}")

    # Assertions
    failures = []

    if final_meta.status != ProjectStatus.EXECUTING:
        failures.append(
            f"Expected final status EXECUTING (Dispatcher completed), "
            f"got {final_meta.status.value}"
        )
    if "write_tasks" not in tool_calls:
        failures.append("Dispatcher never called write_tasks")
    if "mark_dispatch_complete" not in tool_calls:
        failures.append("Dispatcher never called mark_dispatch_complete")
    if len(tasks) == 0:
        failures.append("No tasks written to tasks.json")
    if elapsed > 180:
        failures.append(f"Dispatcher took {elapsed:.0f}s — slower than 3 min, likely a problem")

    # Sanity-check the tasks
    if tasks:
        print()
        print("Tasks produced:")
        for t in tasks:
            ac = t.get("acceptance_criteria", [])
            print(f"  {t['id']}: {t['title']} ({len(ac)} criteria)")
            if len(ac) == 0:
                failures.append(f"Task {t['id']} has no acceptance criteria")
            if not t.get("description"):
                failures.append(f"Task {t['id']} has no description")

    print()
    if failures:
        print("❌ SMOKE TEST FAILED")
        for f in failures:
            print(f"   - {f}")
        return 1
    else:
        print(f"✓ SMOKE TEST PASSED in {elapsed:.1f}s — Dispatcher works end-to-end")
        return 0


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        rc = asyncio.run(run_smoke_test(Path(tmp)))
    sys.exit(rc)
