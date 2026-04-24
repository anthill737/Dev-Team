"""The Orchestrator drives a project through its lifecycle.

v1 implements the interview loop and plan-approval handoff. The execution loop (Dispatcher →
Coder → Reviewer) is stubbed here and will be fleshed out in subsequent sessions.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from ..agents.api_runner import APIRunner
from ..agents.base import (
    AgentRunner,
    ContentBlock,
    Message,
    StreamEvent,
    TextBlock,
)
from ..config import get_settings
from ..prompts import architect_prompt, dispatcher_prompt
from ..state import ProjectPhase, ProjectStatus, ProjectStore
from ..tools import build_architect_tools, build_dispatcher_tools
from .phases import parse_phases

logger = logging.getLogger(__name__)


@dataclass
class ArchitectTurnResult:
    text: str
    tokens_input: int
    tokens_output: int
    status_after: ProjectStatus


class Orchestrator:
    """Coordinates agent activity for a single project.

    Lifecycle:
      - `start_interview(first_user_message)` — kicks off the architect interview
      - `continue_interview(user_message)` — subsequent turns
      - `approve_plan()` — user approves, begins execution (v2 hooks in here)
      - `reject_plan(feedback)` — user sends feedback; architect revises
      - `pause()` / `resume()` — user controls
    """

    def __init__(self, store: ProjectStore, runner: AgentRunner) -> None:
        self.store = store
        self.runner = runner
        self.settings = get_settings()

    # -------------------------------------------------------------------------
    # Interview
    # -------------------------------------------------------------------------

    async def stream_architect_turn(
        self, user_message: str
    ) -> AsyncIterator[StreamEvent]:
        """Stream one architect turn in response to a user message.

        Yields StreamEvent objects for the frontend WebSocket. The final event is
        `turn_complete` with the AgentRunResult in its payload.
        """
        meta = self.store.read_meta()
        # First interview message bumps us from INIT to INTERVIEW
        if meta.status == ProjectStatus.INIT:
            meta.status = ProjectStatus.INTERVIEW
            self.store.write_meta(meta)
            await self.store.append_decision(
                {"actor": "orchestrator", "kind": "state_change", "to": "interview"}
            )

        if meta.status not in (ProjectStatus.INTERVIEW, ProjectStatus.PLANNING):
            yield StreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"Architect is not active — project is in {meta.status.value}. "
                        "Use approve_plan or reject_plan instead."
                    )
                },
            )
            return

        # Log the user message into the interview transcript. Empty string means
        # "resume with whatever's already in the log" — used after reject_plan seeds
        # the feedback directly, so appending here would duplicate it.
        if user_message:
            await self.store.append_interview("user", user_message)

        # Build message history from prior interview turns
        interview_log = self.store.read_interview()
        messages: list[Message] = [
            Message(role=entry["role"], content=[TextBlock(text=entry["content"])])
            for entry in interview_log
        ]

        tools = build_architect_tools(self.store)

        # Incremental mode: if the project already has a plan and at least one
        # completed phase, the Architect should treat this as add-work rather
        # than a fresh interview. The prompt variant tells it to read plan.md,
        # only interview about incremental work, and APPEND a new phase.
        existing_plan = self.store.read_plan()
        has_completed_phase = any(
            p.status == "done" for p in self.store.read_meta().phases
        )
        incremental = bool(existing_plan.strip()) and has_completed_phase

        # Track final text so we can persist it after the stream completes
        collected_text: list[str] = []
        final_result = None

        async for event in self.runner.stream(
            role="architect",
            model=self.settings.model_architect,
            system_prompt=architect_prompt(incremental=incremental),
            messages=messages,
            tools=tools,
            # 32k gives plenty of room for the biggest response the Architect emits
            # (the plan write, ~3-5k tokens in practice). Opus 4.7 supports up to
            # 128k but there's no benefit — the model stops when it's done.
            max_tokens=32000,
            max_iterations=12,
        ):
            if event.kind == "text_delta":
                collected_text.append(event.payload.get("text", ""))
            elif event.kind == "turn_complete":
                final_result = event.payload.get("result")
            yield event

        # Persist assistant turn
        assistant_text = "".join(collected_text).strip()
        if assistant_text:
            await self.store.append_interview("assistant", assistant_text)

        # Update meta with token usage
        if final_result is not None:
            self.store.add_token_usage(
                model=self.settings.model_architect,
                tokens_input=final_result.tokens_input,
                tokens_output=final_result.tokens_output,
                cache_read=final_result.cache_read_tokens,
                cache_creation=final_result.cache_creation_tokens,
            )
            await self.store.append_agent_log(
                {
                    "role": "architect",
                    "tokens_input": final_result.tokens_input,
                    "tokens_output": final_result.tokens_output,
                    "tool_calls": final_result.tool_calls_made,
                    "stop_reason": final_result.stop_reason,
                }
            )

    # -------------------------------------------------------------------------
    # Dispatcher
    # -------------------------------------------------------------------------

    async def stream_dispatcher_turn(self) -> AsyncIterator[StreamEvent]:
        """Stream a Dispatcher run for the current phase.

        Unlike the architect, the dispatcher is invoked fresh (no prior conversation) with a
        single user message telling it which phase to decompose. Streaming events are yielded
        for live UI updates, identical to the architect's stream shape.
        """
        meta = self.store.read_meta()
        if meta.status != ProjectStatus.DISPATCHING:
            yield StreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"Dispatcher not active — project is in {meta.status.value}, "
                        f"expected dispatching."
                    )
                },
            )
            return
        if not meta.current_phase:
            yield StreamEvent(
                kind="error",
                payload={"message": "No current phase set — cannot dispatch."},
            )
            return

        phase_id = meta.current_phase
        phase_title = next(
            (p.title for p in meta.phases if p.id == phase_id), phase_id
        )

        kickoff_message = Message(
            role="user",
            content=[
                TextBlock(
                    text=(
                        f"Decompose phase {phase_id} ({phase_title!r}) into tasks. "
                        f"Call read_phase first, then call write_tasks with the committed "
                        f"list, then call mark_dispatch_complete. Use ids {phase_id}-T1, "
                        f"{phase_id}-T2, etc. Be fast — commit tasks with tool calls, not "
                        f"with long prose."
                    )
                )
            ],
        )

        tools = build_dispatcher_tools(self.store, phase_id=phase_id)

        await self.store.append_decision(
            {"actor": "orchestrator", "kind": "dispatcher_start", "phase": phase_id}
        )

        final_result = None
        async for event in self.runner.stream(
            role="dispatcher",
            model=self.settings.model_dispatcher,
            system_prompt=dispatcher_prompt(),
            messages=[kickoff_message],
            tools=tools,
            # 64k is Sonnet 4.6's hard per-response cap. Setting it here means the
            # Dispatcher can never truncate mid-task-list on a dense plan. (The
            # project-level 2M budget tracks CUMULATIVE usage across all calls;
            # this is a PER-CALL ceiling on one response, a different thing.)
            # Cost note: max_tokens is a ceiling, not a target — the model stops
            # when it's done. A typical task list costs <10k output tokens even
            # with a 64k cap.
            max_tokens=64000,
            # The Dispatcher for a dense single-phase plan may need many iterations —
            # read_plan, read_phase, draft tasks, hit validation, revise, etc. A strict
            # iteration cap caused false blocks on real projects. Let it think; the
            # wall-clock below is the real runaway guard.
            max_iterations=200,
            wall_clock_seconds=300.0,  # 5 min hard ceiling — if nothing's worked by then, something is wrong
        ):
            if event.kind == "turn_complete":
                final_result = event.payload.get("result")
            yield event

        if final_result is not None:
            self.store.add_token_usage(
                model=self.settings.model_dispatcher,
                tokens_input=final_result.tokens_input,
                tokens_output=final_result.tokens_output,
                cache_read=final_result.cache_read_tokens,
                cache_creation=final_result.cache_creation_tokens,
            )
            await self.store.append_agent_log(
                {
                    "role": "dispatcher",
                    "phase": phase_id,
                    "tokens_input": final_result.tokens_input,
                    "tokens_output": final_result.tokens_output,
                    "tool_calls": final_result.tool_calls_made,
                    "stop_reason": final_result.stop_reason,
                }
            )

        # If the dispatcher didn't transition us to EXECUTING (it should have via
        # mark_dispatch_complete), flag it as BLOCKED so the user knows something's wrong.
        final_meta = self.store.read_meta()
        if final_meta.status == ProjectStatus.DISPATCHING:
            final_meta.status = ProjectStatus.BLOCKED
            self.store.write_meta(final_meta)
            # Build an actually-useful reason from what we know. The user shouldn't
            # have to cross-reference the decisions log to understand what broke.
            if final_result is None:
                reason = "Dispatcher crashed before completing a turn"
                stop_reason_detail = None
                tool_calls_made = 0
            else:
                stop_reason_detail = final_result.stop_reason
                tool_calls_made = final_result.tool_calls_made
                if stop_reason_detail == "wall_clock_timeout":
                    reason = (
                        "Dispatcher hit the 5-minute wall-clock timeout without "
                        f"completing. Made {tool_calls_made} tool calls."
                    )
                elif stop_reason_detail == "max_iterations":
                    reason = (
                        f"Dispatcher hit max iterations after {tool_calls_made} tool "
                        "calls without calling mark_dispatch_complete."
                    )
                elif stop_reason_detail == "max_tokens":
                    reason = (
                        f"Dispatcher's response was cut off by max_tokens after "
                        f"{tool_calls_made} tool calls — the task list was too long "
                        "to write in one call. Retry; the Dispatcher has more room now."
                    )
                else:
                    reason = (
                        f"Dispatcher stopped (reason: {stop_reason_detail}) after "
                        f"{tool_calls_made} tool calls without calling "
                        "mark_dispatch_complete."
                    )
            await self.store.append_decision(
                {
                    "actor": "orchestrator",
                    "kind": "dispatcher_blocked",
                    "reason": reason,
                    "stop_reason": stop_reason_detail,
                    "tool_calls": tool_calls_made,
                }
            )

    # -------------------------------------------------------------------------
    # Approval
    # -------------------------------------------------------------------------

    async def stream_execution_loop(self) -> AsyncIterator[StreamEvent]:
        """Drive the execution loop for an approved phase.

        Builds the Coder + sandbox + ExecutionLoop and streams events out to the
        WebSocket. Requires the project to be in DISPATCHING (just dispatched) or
        EXECUTING (resuming after restart). Exits cleanly on phase complete, project
        complete, blocked, paused, or deadlock — the frontend's status polling picks
        up the state change and navigates accordingly.
        """
        from ..agents.coder import Coder
        from ..sandbox import ProcessSandboxExecutor
        from .execution_loop import ExecutionLoop

        meta = self.store.read_meta()
        if meta.status not in (ProjectStatus.EXECUTING, ProjectStatus.DISPATCHING):
            yield StreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"Execution loop not available — project is in "
                        f"{meta.status.value}, expected executing or dispatching."
                    )
                },
            )
            return

        try:
            sandbox = ProcessSandboxExecutor(self.store.root)
        except ValueError as exc:
            yield StreamEvent(
                kind="error",
                payload={"message": f"Cannot create sandbox: {exc}"},
            )
            return

        coder = Coder(runner=self.runner, model=self.settings.model_coder)
        # Reviewer is always constructed; ExecutionLoop only invokes it for tasks
        # the Dispatcher flagged with requires_review=True. For tasks without the
        # flag, the Reviewer is never called, so there's no cost penalty to having
        # it instantiated.
        from ..agents.reviewer import Reviewer

        reviewer = Reviewer(runner=self.runner, model=self.settings.model_reviewer)
        loop = ExecutionLoop(
            store=self.store,
            sandbox=sandbox,
            runner=coder,
            coder_model=self.settings.model_coder,
            reviewer=reviewer,
            reviewer_model=self.settings.model_reviewer,
        )

        await self.store.append_decision(
            {"actor": "orchestrator", "kind": "execution_loop_start"}
        )

        async for event in loop.run():
            yield event

        await self.store.append_decision(
            {"actor": "orchestrator", "kind": "execution_loop_end"}
        )

    async def approve_plan(self) -> str | None:
        """User approves the plan. Parse phases, transition to DISPATCHING.

        Returns the phase_id ready for dispatch (or None if plan has no parseable phases).

        Incremental-mode aware: if the project has phases already completed (from
        a prior add-work cycle or project_complete), their status is preserved.
        Only the newly-appended phase(s) dispatch. If every phase parses as done,
        we re-complete the project without re-running anything.
        """
        meta = self.store.read_meta()
        if meta.status != ProjectStatus.AWAIT_APPROVAL:
            raise RuntimeError(
                f"Cannot approve plan — project is in {meta.status.value}, not await_approval"
            )

        # Parse phases from the plan so we know what to dispatch
        plan = self.store.read_plan()
        parsed = parse_phases(plan)
        if not parsed:
            # Fall back: treat the whole plan as a single phase "P1"
            parsed = [type("_P", (), {"id": "P1", "title": "Implement MVP"})()]

        # Preserve status/approval of any previously-completed phases. This is
        # what makes incremental (add-work) flow safe: P1 stays marked done and
        # its tasks stay done; only the new phase(s) become active.
        prior_by_id = {p.id: p for p in meta.phases}
        new_phases: list[ProjectPhase] = []
        for p in parsed:
            prior = prior_by_id.get(p.id)
            if prior and prior.status == "done":
                # Keep done phases fully intact
                new_phases.append(prior)
            else:
                new_phases.append(
                    ProjectPhase(
                        id=p.id, title=p.title, status="pending", approved_by_user=False
                    )
                )
        meta.phases = new_phases

        # Find the first non-done phase; that's the one the Dispatcher picks up.
        # If every phase is already done (weird edge case — user approved an
        # add-work plan that somehow didn't add anything), just re-mark complete.
        first_pending = next(
            (p for p in meta.phases if p.status != "done"), None
        )
        if first_pending is None:
            meta.status = ProjectStatus.COMPLETE
            meta.current_phase = None
            self.store.write_meta(meta)
            await self.store.append_decision(
                {
                    "actor": "user",
                    "kind": "plan_approved_noop",
                    "note": "Plan approved but all phases already complete — nothing to dispatch.",
                }
            )
            return None

        first_pending.approved_by_user = True
        first_pending.status = "active"
        meta.current_phase = first_pending.id
        meta.status = ProjectStatus.DISPATCHING
        self.store.write_meta(meta)

        await self.store.append_decision(
            {
                "actor": "user",
                "kind": "plan_approved",
                "phase_count": len(meta.phases),
                "first_phase": meta.current_phase,
            }
        )
        return meta.current_phase

    async def reject_plan(self, feedback: str) -> None:
        """User rejects the plan with feedback. Return to interview for revisions."""
        meta = self.store.read_meta()
        if meta.status != ProjectStatus.AWAIT_APPROVAL:
            raise RuntimeError(
                f"Cannot reject plan — project is in {meta.status.value}, not await_approval"
            )
        meta.status = ProjectStatus.INTERVIEW
        self.store.write_meta(meta)
        await self.store.append_decision(
            {"actor": "user", "kind": "plan_rejected", "feedback": feedback}
        )
        # Seed the interview with the user's feedback so the architect sees it on next turn
        await self.store.append_interview(
            "user",
            f"[Plan feedback — revise accordingly]\n\n{feedback}",
        )

    # -------------------------------------------------------------------------
    # Pause / resume
    # -------------------------------------------------------------------------

    async def pause(self) -> None:
        meta = self.store.read_meta()
        meta.status = ProjectStatus.PAUSED
        self.store.write_meta(meta)
        await self.store.append_decision({"actor": "user", "kind": "paused"})

    async def resume(self, resume_to: ProjectStatus) -> None:
        meta = self.store.read_meta()
        meta.status = resume_to
        self.store.write_meta(meta)
        await self.store.append_decision(
            {"actor": "user", "kind": "resumed", "to": resume_to.value}
        )

    async def retry_dispatcher(self) -> None:
        """Re-run the Dispatcher from a BLOCKED state.

        Only valid when the project is BLOCKED and has a current_phase set. Flips
        status back to DISPATCHING so the frontend's dispatcher WebSocket re-opens
        and a fresh Dispatcher run kicks off. Existing tasks.json is preserved —
        write_tasks appends rather than replacing, so if the prior dispatcher did
        partial work it isn't lost.
        """
        meta = self.store.read_meta()
        if meta.status != ProjectStatus.BLOCKED:
            raise RuntimeError(
                f"Cannot retry dispatcher — project is in {meta.status.value}, not blocked."
            )
        if not meta.current_phase:
            raise RuntimeError(
                "Cannot retry dispatcher — no current phase set."
            )
        meta.status = ProjectStatus.DISPATCHING
        self.store.write_meta(meta)
        await self.store.append_decision(
            {
                "actor": "user",
                "kind": "dispatcher_retry",
                "phase": meta.current_phase,
            }
        )
