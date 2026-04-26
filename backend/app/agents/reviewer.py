"""The Reviewer agent: skeptical quality gate for Coder work.

Runs after the Coder signals a task done IF the Dispatcher marked the task with
requires_review=True. Reads the code and tests, runs them in the sandbox, and
decides approve or request_changes. On request_changes, the task goes back to
the Coder with the findings as rework notes. Bounded by max_review_cycles so we
never loop forever.

Design contract:
  - Yields StreamEvents like the Coder (text_delta, tool_use, tool_result, usage)
  - Emits exactly one `review_outcome` event before returning, carrying a ReviewResult
  - Honors a wall-clock timeout so a hang can't block the execution loop
  - Uses Opus by default — the paper's key finding is that an evaluator needs
    better judgment than the generator, and Opus prices the extra cost in

Implementation: wrap APIRunner.stream() and watch for submit_review's signal the
same way the Coder watches signal_outcome.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..agents.base import AgentRunner, Message, StreamEvent, TextBlock
from ..prompts import reviewer_prompt
from ..sandbox import SandboxExecutor
from ..state import ProjectStore
from ..tools.reviewer_tools import ReviewSignal, build_reviewer_tools

logger = logging.getLogger(__name__)


# Reviews shouldn't take as long as Coder runs — less output, mostly reading.
# 6 minutes is ample for the Reviewer to read relevant files, run the tests,
# and submit a verdict. If it blows this, something is wrong.
DEFAULT_REVIEW_WALL_CLOCK_SECONDS = 6 * 60


@dataclass
class ReviewResult:
    """Structured outcome of one review pass.

    The execution loop interprets this into a TaskOutcome downstream:
      - approve → task marked done
      - request_changes → task status reset to pending, iterations+1, notes include findings
      - error → task BLOCKED with error details (reviewer hit timeout, crashed, etc.)
    """

    kind: str  # "approve" | "request_changes" | "error"
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    error_reason: str = ""


class Reviewer:
    """Skeptical Reviewer agent — verifies Coder's work against acceptance criteria."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        model: str = "claude-opus-4-7",
        wall_clock_seconds: int = DEFAULT_REVIEW_WALL_CLOCK_SECONDS,
    ) -> None:
        self._runner = runner
        self._model = model
        self._wall_clock = wall_clock_seconds

    async def run(
        self,
        *,
        store: ProjectStore,
        sandbox: SandboxExecutor,
        task: dict,
    ) -> AsyncIterator[StreamEvent]:
        """Run one review pass. Yields events; emits a `review_outcome` event before returning."""

        # Shared signal slot — submit_review writes here, we read after the loop.
        signal_slot: dict[str, ReviewSignal | None] = {"signal": None}

        def _receiver(sig: ReviewSignal) -> None:
            signal_slot["signal"] = sig

        # Read meta once at the start of the review. We need playwright_enabled
        # for tool registration and prompt rendering, and user_platform for the
        # platform-specific prompt hints. Reading both at once keeps the
        # snapshot consistent — if the user toggles playwright mid-review (rare
        # but possible), this review uses the value at start-of-review.
        meta = store.read_meta()
        user_platform = meta.user_platform
        playwright_enabled = bool(meta.playwright_enabled)

        tools = build_reviewer_tools(
            store=store,
            sandbox=sandbox,
            task=task,
            signal_receiver=_receiver,
            playwright_enabled=playwright_enabled,
        )

        # Initial-message framing — short, points the Reviewer at the task and
        # restates the workflow. The system prompt has the heavy detail; this
        # is just enough to anchor the first turn. When Playwright is on, we
        # mention playwright_check explicitly so the Reviewer knows the tool
        # is available without having to scan the system prompt for it.
        playwright_line = (
            "  5. For browser-rendered artifacts: call playwright_check on the "
            "URL and verify the page actually renders without console errors.\n"
            if playwright_enabled
            else "  5. Where you can, actually exercise the behavior (run the command, curl the endpoint).\n"
        )
        initial_text = (
            f"Task {task['id']} ('{task['title']}') has been marked done by the Coder "
            f"and flagged for your review. Verify it against the acceptance criteria.\n\n"
            f"Process:\n"
            f"  1. Call read_task first — see the criteria + any findings from prior review cycles.\n"
            f"  2. Read the Coder's code (fs_list, fs_read) — the new files AND their integration points.\n"
            f"  3. Read the tests — do they actually test the behavior, or are they mocks of the thing being tested?\n"
            f"  4. Run the tests yourself with bash (don't trust the Coder's claim they pass).\n"
            f"{playwright_line}"
            f"  6. Call submit_review with your verdict.\n\n"
            f"Default posture: find a bug. Hard threshold: any confirmed shortfall = request_changes."
        )
        messages: list[Message] = [
            Message(role="user", content=[TextBlock(text=initial_text)])
        ]

        tokens_input_total = 0
        tokens_output_total = 0
        cache_read_total = 0
        cache_creation_total = 0

        try:
            async for ev in _timeout_wrapped_stream(
                self._stream_inner(
                    messages=messages,
                    tools=tools,
                    signal_slot=signal_slot,
                    user_platform=user_platform,
                    playwright_enabled=playwright_enabled,
                ),
                timeout_seconds=self._wall_clock,
            ):
                if ev.kind == "usage":
                    tokens_input_total += int(ev.payload.get("input_tokens", 0))
                    tokens_output_total += int(ev.payload.get("output_tokens", 0))
                    cache_read_total += int(ev.payload.get("cache_read_tokens", 0))
                    cache_creation_total += int(
                        ev.payload.get("cache_creation_tokens", 0)
                    )
                yield ev
        except asyncio.TimeoutError:
            yield StreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"Reviewer exceeded wall-clock timeout ({self._wall_clock}s) "
                        f"for task {task['id']}"
                    )
                },
            )
            result = ReviewResult(
                kind="error",
                error_reason=f"Reviewer run exceeded {self._wall_clock}s wall clock",
                tokens_input=tokens_input_total,
                tokens_output=tokens_output_total,
                cache_read_tokens=cache_read_total,
                cache_creation_tokens=cache_creation_total,
            )
            yield StreamEvent(kind="review_outcome", payload={"result": result})
            return

        # Resolve verdict. If the Reviewer didn't call submit_review (ran to end of
        # turn without submitting), that's an error — the review is inconclusive.
        sig = signal_slot["signal"]
        if sig is None:
            logger.warning(
                "Reviewer for task %s did not call submit_review; marking as error",
                task["id"],
            )
            result = ReviewResult(
                kind="error",
                error_reason=(
                    "Reviewer ended turn without calling submit_review. Verdict "
                    "inconclusive — review cycle consumed but no decision."
                ),
                tokens_input=tokens_input_total,
                tokens_output=tokens_output_total,
                cache_read_tokens=cache_read_total,
                cache_creation_tokens=cache_creation_total,
            )
        else:
            result = ReviewResult(
                kind=sig.outcome,
                summary=sig.summary,
                findings=sig.findings,
                tokens_input=tokens_input_total,
                tokens_output=tokens_output_total,
                cache_read_tokens=cache_read_total,
                cache_creation_tokens=cache_creation_total,
            )

        yield StreamEvent(kind="review_outcome", payload={"result": result})

    async def _stream_inner(
        self,
        *,
        messages: list[Message],
        tools: list,
        signal_slot: dict,
        user_platform: str,
        playwright_enabled: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Run the APIRunner stream; short-circuit once submit_review fires."""
        async for ev in self._runner.stream(
            role="reviewer",
            model=self._model,
            system_prompt=reviewer_prompt(
                user_platform=user_platform,
                playwright_enabled=playwright_enabled,
            ),
            messages=messages,
            tools=tools,
            max_tokens=16000,  # Reviews are mostly reading, not writing
            max_iterations=25,
        ):
            yield ev
            if (
                ev.kind == "tool_result"
                and ev.payload.get("name") == "submit_review"
                and signal_slot["signal"] is not None
            ):
                return


async def _timeout_wrapped_stream(
    gen: AsyncIterator[StreamEvent], *, timeout_seconds: int
) -> AsyncIterator[StreamEvent]:
    """Same deadline-per-event pattern as Coder._timeout_wrapped_stream."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    agen = gen.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        try:
            ev = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        yield ev
