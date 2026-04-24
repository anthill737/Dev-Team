"""The Coder agent: reads a task, writes files, runs tests, iterates until outcome.

This class implements the `TaskRunner` protocol. The execution loop hands it a task via
`TaskContext` and consumes the events it yields. The Coder's contract:

  - Drive an agentic loop: read_task → explore → write files → run tests → iterate
  - Self-enforce the task's token budget (the execution loop trusts us on this)
  - Emit a final `task_outcome` event with a TaskOutcome payload before returning
  - Honor a wall-clock timeout so a hung turn can't deadlock the loop

Implementation strategy: wrap APIRunner.stream() so the Coder gets the full tool-use loop
for free. We intercept events to watch for three things:

  1. `usage` events — to enforce the token budget
  2. The signal_outcome tool being called — to short-circuit the loop
  3. Timeouts — wrap the whole thing in asyncio.wait_for
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ..agents.base import AgentRunner, Message, StreamEvent, TextBlock
from ..orchestrator.task_runner import TaskContext, TaskOutcome, TaskOutcomeKind
from ..prompts import coder_prompt
from ..tools.coder_tools import build_coder_tools

logger = logging.getLogger(__name__)


# Hard wall-clock cap per task. If the Coder hasn't finished in 10 minutes, something
# is very wrong — hung subprocess, infinite loop in the model, network stall. Kill it
# and return FAILED so the orchestrator can escalate.
DEFAULT_TASK_WALL_CLOCK_SECONDS = 10 * 60


# Budget enforcement margin. We stop issuing new turns when the Coder has consumed
# this fraction of its budget — leaves some room for the final signal_outcome call.
_BUDGET_CUTOFF_FRACTION = 0.90


class Coder:
    """TaskRunner implementation backed by the APIRunner."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        model: str = "claude-sonnet-4-6",
        wall_clock_seconds: int = DEFAULT_TASK_WALL_CLOCK_SECONDS,
    ) -> None:
        self._runner = runner
        self._model = model
        self._wall_clock = wall_clock_seconds

    async def run(self, ctx: TaskContext) -> AsyncIterator[StreamEvent]:
        """Run one task attempt. Yields events. Must emit a task_outcome event before returning."""

        # Shared outcome slot — the signal_outcome tool writes here, we read it after
        # the APIRunner loop completes.
        signaled: dict[str, TaskOutcome | None] = {"outcome": None}

        def _receiver(outcome: TaskOutcome) -> None:
            signaled["outcome"] = outcome

        tools = build_coder_tools(
            store=ctx.store,
            sandbox=ctx.sandbox,
            task=ctx.task,
            outcome_receiver=_receiver,
        )

        # Build the user message telling the Coder what task to work on
        initial_text = (
            f"Task {ctx.task['id']} is assigned to you. Call read_task first to see "
            f"its full spec and acceptance criteria. Then explore the project with "
            f"fs_list and fs_read, write code with fs_write, run tests with bash, and "
            f"iterate until acceptance criteria are met. Call signal_outcome exactly "
            f"once when you're done.\n\n"
            f"Budget: you have ~{ctx.task_token_budget} tokens for this task. Be "
            f"efficient — don't read every file in the project if you don't need to."
        )

        messages: list[Message] = [
            Message(role="user", content=[TextBlock(text=initial_text)])
        ]
        tokens_used = 0
        # Track cache read/creation across turns so we can bucket them correctly
        # for cost display. These are subsets of tokens_used, so the budget check
        # is unaffected. cache_read bills at 10% of input, cache_creation at 125%.
        cache_read_total = 0
        cache_creation_total = 0

        try:
            # Wrap the entire inner run in a wall-clock timeout so a hang can't deadlock
            # the orchestrator. The inner function produces events as a list (drained from
            # the APIRunner stream) — a generator directly wrapped in wait_for behaves
            # oddly across suspension points.
            async for ev in _timeout_wrapped_stream(
                self._stream_inner(
                    ctx=ctx, messages=messages, tools=tools, signaled=signaled
                ),
                timeout_seconds=self._wall_clock,
            ):
                # Accumulate token usage for our own budget check
                if ev.kind == "usage":
                    tokens_used += int(ev.payload.get("input_tokens", 0))
                    tokens_used += int(ev.payload.get("output_tokens", 0))
                    cache_read_total += int(ev.payload.get("cache_read_tokens", 0))
                    cache_creation_total += int(ev.payload.get("cache_creation_tokens", 0))

                yield ev

                # Self-enforce budget: if we're over the cutoff, stop pumping events.
                # The inner generator will still run to completion inside APIRunner, but
                # we stop passing through, and we'll emit a BLOCKED outcome below.
                if (
                    signaled["outcome"] is None
                    and tokens_used >= ctx.task_token_budget * _BUDGET_CUTOFF_FRACTION
                ):
                    logger.warning(
                        "Coder task %s hit budget cutoff at %d tokens",
                        ctx.task["id"],
                        tokens_used,
                    )
                    break
        except asyncio.TimeoutError:
            yield StreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"Task {ctx.task['id']} exceeded wall-clock timeout "
                        f"({self._wall_clock}s)"
                    )
                },
            )
            outcome = TaskOutcome(
                kind=TaskOutcomeKind.FAILED,
                tokens_input=0,
                tokens_output=0,
                failure_reason=f"Coder run exceeded {self._wall_clock}s wall clock",
            )
            yield StreamEvent(kind="task_outcome", payload={"outcome": outcome})
            return

        # Decide the outcome: prefer what the Coder signaled; otherwise synthesize one.
        if signaled["outcome"] is not None:
            outcome = signaled["outcome"]
            # Attribute accumulated tokens to the outcome so the execution loop can
            # update the project total. Split evenly between input/output is imprecise,
            # but the only thing that matters for budgeting is the total.
            if outcome.tokens_input == 0 and outcome.tokens_output == 0:
                outcome.tokens_input = tokens_used
            outcome.cache_read_tokens = cache_read_total
            outcome.cache_creation_tokens = cache_creation_total
        else:
            # The Coder ran to end-of-turn without calling signal_outcome, OR we broke
            # out above because we hit the budget cutoff. Either way, no clean outcome.
            if tokens_used >= ctx.task_token_budget * _BUDGET_CUTOFF_FRACTION:
                reason = (
                    f"Exhausted task token budget (~{tokens_used} of "
                    f"{ctx.task_token_budget}) without signaling an outcome"
                )
                outcome = TaskOutcome(
                    kind=TaskOutcomeKind.BLOCKED,
                    tokens_input=tokens_used,
                    cache_read_tokens=cache_read_total,
                    cache_creation_tokens=cache_creation_total,
                    block_reason=reason,
                )
            else:
                outcome = TaskOutcome(
                    kind=TaskOutcomeKind.FAILED,
                    tokens_input=tokens_used,
                    cache_read_tokens=cache_read_total,
                    cache_creation_tokens=cache_creation_total,
                    failure_reason="Coder ended turn without calling signal_outcome",
                )

        yield StreamEvent(kind="task_outcome", payload={"outcome": outcome})

    async def _stream_inner(
        self,
        *,
        ctx: TaskContext,
        messages: list[Message],
        tools,
        signaled: dict[str, TaskOutcome | None],
    ) -> AsyncIterator[StreamEvent]:
        """The actual APIRunner stream, pass-through with early exit when outcome signaled."""
        # Read platform fresh from disk each run so a mid-project platform
        # override (user edits via PATCH) takes effect on the next iteration.
        user_platform = ctx.store.read_meta().user_platform
        async for ev in self._runner.stream(
            role="coder",
            model=self._model,
            system_prompt=coder_prompt(user_platform=user_platform),
            messages=messages,
            tools=tools,
            # 32k so the Coder can write a substantial file in one fs_write call
            # without running out of response space. Sonnet 4.6 caps at 64k.
            max_tokens=32000,
            # Give it room for genuine multi-step work
            max_iterations=40,
        ):
            yield ev

            # If the Coder called signal_outcome, the tool result has already been
            # sent back into the model, but there's no point running another turn.
            # Short-circuit after we see the tool_result for signal_outcome.
            if (
                ev.kind == "tool_result"
                and ev.payload.get("name") == "signal_outcome"
                and signaled["outcome"] is not None
            ):
                return


async def _timeout_wrapped_stream(
    gen: AsyncIterator[StreamEvent], *, timeout_seconds: int
) -> AsyncIterator[StreamEvent]:
    """Re-yield events from `gen` but raise asyncio.TimeoutError if total time exceeds limit.

    asyncio.wait_for around a generator is awkward; we apply the timeout per-event by
    tracking the deadline and using wait_for on each anext call. If a single event takes
    longer than remaining time, we raise TimeoutError and the caller handles it.
    """
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
