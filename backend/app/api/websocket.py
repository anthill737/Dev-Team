"""WebSocket route for live architect interview streaming.

Protocol (all messages are JSON):

Client -> Server:
  { "type": "user_message", "text": "..." }

Server -> Client:
  { "type": "text_delta", "text": "..." }         # streaming architect text
  { "type": "tool_use", "name": "...", "input": {...} }
  { "type": "tool_result", "name": "...", "is_error": bool, "preview": "..." }
  { "type": "usage", "input_tokens": N, "output_tokens": N }
  { "type": "turn_complete", "status": "..." }
  { "type": "error", "message": "..." }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..orchestrator import Orchestrator
from ..state import ProjectStatus, ProjectStore
from .projects import _load_registry
from .session import _store as _key_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/architect/{project_id}")
async def architect_ws(websocket: WebSocket, project_id: str) -> None:
    """WebSocket for streaming architect interview turns."""
    await _run_agent_ws(websocket, project_id, agent="architect")


@router.websocket("/dispatcher/{project_id}")
async def dispatcher_ws(websocket: WebSocket, project_id: str) -> None:
    """WebSocket for streaming a Dispatcher run.

    Unlike the architect WS (which is a multi-turn conversation), the dispatcher runs
    once per phase. The client opens this socket when project status is DISPATCHING;
    the backend runs the dispatcher turn and closes the socket when complete.
    """
    await _run_agent_ws(websocket, project_id, agent="dispatcher")


@router.websocket("/execution/{project_id}")
async def execution_ws(websocket: WebSocket, project_id: str) -> None:
    """WebSocket for streaming the execution loop (Coder driving tasks to completion).

    Client opens this when project status is EXECUTING. Backend runs the loop until
    a terminal state (phase_review, complete, blocked, paused, deadlock) then closes.
    Long-lived — a single phase can take many minutes.
    """
    await _run_agent_ws(websocket, project_id, agent="execution")


async def _run_agent_ws(websocket: WebSocket, project_id: str, *, agent: str) -> None:
    # Authenticate
    api_key = _key_store.get()
    if api_key is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="no_api_key")
        return

    # Find project
    registry = _load_registry()
    root_path = None
    for entry in registry:
        if entry["id"] == project_id:
            root_path = entry["root_path"]
            break
    if root_path is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="project_not_found")
        return

    store = ProjectStore(root_path)
    # Pick runner based on global config — claude_code uses subscription,
    # api uses the in-memory API key. Avoids having to duplicate selection
    # logic; matches every other orchestrator instantiation site.
    from .projects import _build_runner
    orchestrator = Orchestrator(store=store, runner=_build_runner(store))

    await websocket.accept()
    logger.info("WS accepted for project %s (agent=%s)", project_id, agent)

    try:
        if agent == "architect":
            await _architect_loop(websocket, orchestrator, store, project_id)
        elif agent == "dispatcher":
            await _dispatcher_oneshot(websocket, orchestrator, store, project_id)
        elif agent == "execution":
            await _execution_loop(websocket, orchestrator, store, project_id)
        else:
            await websocket.send_text(
                json.dumps({"type": "error", "message": f"Unknown agent: {agent}"})
            )
    except WebSocketDisconnect:
        logger.info("WS disconnected for project %s (agent=%s)", project_id, agent)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WS error for project %s (agent=%s)", project_id, agent)
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason=str(exc)[:120])
        except Exception:  # noqa: BLE001
            pass


async def _architect_loop(
    websocket: WebSocket, orchestrator: Orchestrator, store: ProjectStore, project_id: str
) -> None:
    """Multi-turn conversation loop — client sends user_message, server streams response.

    On connect, we check whether the interview log ends with an unanswered user
    message. This happens after reject_plan seeds the user's feedback: the status
    flips back to INTERVIEW, the feedback is appended as a user turn, but nothing
    fires the Architect to respond. By detecting that state on WS connect and
    driving a turn with empty user_message (which tells stream_architect_turn not
    to re-append), we give the user an automatic Architect response the moment
    they return to the project — no retyping needed.
    """
    # Resume-after-rejection: if the interview log's last entry is a user message
    # with no subsequent assistant response, fire a turn now.
    interview_log = store.read_interview()
    needs_resume = (
        len(interview_log) > 0
        and interview_log[-1].get("role") == "user"
        and store.read_meta().status == ProjectStatus.INTERVIEW
    )
    if needs_resume:
        logger.info(
            "Architect WS resuming with seeded user message for project %s", project_id
        )
        try:
            async for event in orchestrator.stream_architect_turn(""):
                out = _translate_event(event)
                if out is not None:
                    await _send(websocket, out)
            meta = store.read_meta()
            await _send(
                websocket,
                {
                    "type": "turn_complete",
                    "status": meta.status.value,
                    "tokens_used": meta.tokens_used,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Seeded architect turn failed for project %s", project_id)
            await _send(
                websocket,
                {"type": "error", "message": f"Architect turn failed: {exc}"},
            )

    while True:
        raw = await websocket.receive_text()
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await _send(websocket, {"type": "error", "message": "Invalid JSON"})
            continue

        mtype = message.get("type")
        if mtype != "user_message":
            await _send(
                websocket,
                {"type": "error", "message": f"Unknown message type: {mtype}"},
            )
            continue

        user_text = (message.get("text") or "").strip()
        # Empty user_text is valid ONLY if the interview log already ends with an
        # unanswered user turn (e.g. reject_plan seeded feedback). In that case the
        # frontend sends an empty user_message as a "fire the turn" signal. Otherwise
        # empty is a client error.
        if not user_text:
            log = store.read_interview()
            has_unanswered = (
                len(log) > 0
                and log[-1].get("role") == "user"
                and store.read_meta().status == ProjectStatus.INTERVIEW
            )
            if not has_unanswered:
                await _send(
                    websocket,
                    {"type": "error", "message": "Empty user message"},
                )
                continue

        try:
            async for event in orchestrator.stream_architect_turn(user_text):
                out = _translate_event(event)
                if out is not None:
                    await _send(websocket, out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Architect turn failed for project %s", project_id)
            await _send(
                websocket,
                {"type": "error", "message": f"Architect turn failed: {exc}"},
            )
            continue

        meta = store.read_meta()
        await _send(
            websocket,
            {
                "type": "turn_complete",
                "status": meta.status.value,
                "tokens_used": meta.tokens_used,
            },
        )


async def _execution_loop(
    websocket: WebSocket, orchestrator: Orchestrator, store: ProjectStore, project_id: str
) -> None:
    """View an execution loop running as a background job.

    We are NOT driving the loop here — that's done by the JobRegistry as an
    asyncio task decoupled from any WebSocket. Our only jobs:
      1. If a job exists: replay its buffered events and stream new ones live.
      2. If no job exists but project status is EXECUTING: start one.
         (Handles resume-from-paused, backend restart, and late attachment.)
      3. Disconnect cleanly without stopping the job.
    """
    from ..orchestrator.job_registry import get_registry

    registry = get_registry()

    # Start a job if the project is in an executable state and nothing's running.
    # If there's already a job, ensure_running is a no-op — the existing job stays.
    meta = store.read_meta()
    if (
        meta.status in (ProjectStatus.EXECUTING, ProjectStatus.DISPATCHING)
        and not registry.is_running(project_id)
    ):
        await registry.ensure_running(
            project_id, lambda: orchestrator.stream_execution_loop()
        )

    subscription = await registry.subscribe(project_id)
    if subscription is None:
        # No job exists and project status isn't executable. Tell the client
        # what the current state is so it can reflect it, then close cleanly.
        meta = store.read_meta()
        await _send(
            websocket,
            {
                "type": "turn_complete",
                "status": meta.status.value,
                "tokens_used": meta.tokens_used,
            },
        )
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return

    replay, queue = subscription

    try:
        # Replay buffered events first so the viewer sees the story from where
        # the buffer starts. The wire protocol is the same whether replayed or live.
        for job_ev in replay:
            out = _translate_event(_ReplayEvent(job_ev.kind, job_ev.payload))
            if out is not None:
                await _send(websocket, out)

        # Live stream from the subscription queue until the job signals end (None).
        while True:
            job_ev = await queue.get()
            if job_ev is None:
                break  # Job finished
            out = _translate_event(_ReplayEvent(job_ev.kind, job_ev.payload))
            if out is not None:
                await _send(websocket, out)

        # Job done — send a final turn_complete with the on-disk state.
        meta = store.read_meta()
        await _send(
            websocket,
            {
                "type": "turn_complete",
                "status": meta.status.value,
                "tokens_used": meta.tokens_used,
            },
        )
    except WebSocketDisconnect:
        logger.info(
            "Execution viewer disconnected for project %s; job continues in background",
            project_id,
        )
        # Let the job keep running — it's not tied to this WS.
    finally:
        await registry.unsubscribe(project_id, queue)

    try:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
    except RuntimeError:
        pass  # Already closed


class _ReplayEvent:
    """Duck-typed StreamEvent shim for _translate_event which expects .kind/.payload."""

    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


async def _dispatcher_oneshot(
    websocket: WebSocket, orchestrator: Orchestrator, store: ProjectStore, project_id: str
) -> None:
    """Legacy dispatcher-only WebSocket. Kept for backward compatibility but no
    longer the primary path.

    New model: decide_plan → ensure_background_execution → stream_execution_loop
    runs the dispatcher AND the execution loop together as one background job.
    Clients opening this WS will still see dispatcher events stream, but the
    corresponding background job is the authoritative one for state changes.
    If no background job is running yet, this WS will kick one off via the
    idempotent ensure_running (no double-dispatch because the job registry
    dedupes per project_id).
    """
    from ..orchestrator.job_registry import get_registry

    registry = get_registry()
    if not registry.is_running(project_id):
        # No background job running — start one. It will run dispatcher + execution.
        await registry.ensure_running(
            project_id, lambda: orchestrator.stream_execution_loop()
        )

    # Subscribe to the running job just like the execution WS does; let the
    # viewer see the stream.
    subscription = await registry.subscribe(project_id)
    if subscription is None:
        await _send(
            websocket,
            {"type": "error", "message": "Could not attach to dispatcher job"},
        )
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return
    replay, queue = subscription

    try:
        for job_ev in replay:
            out = _translate_event(_ReplayEvent(job_ev.kind, job_ev.payload))
            if out is not None:
                await _send(websocket, out)
        # Dispatcher WS historically closes after dispatcher turn completes. We
        # detect that by watching for status flip to EXECUTING (or terminal).
        while True:
            job_ev = await queue.get()
            if job_ev is None:
                break
            out = _translate_event(_ReplayEvent(job_ev.kind, job_ev.payload))
            if out is not None:
                await _send(websocket, out)
            # Close this WS once dispatcher is done — frontend expects one-shot.
            meta = store.read_meta()
            if meta.status != ProjectStatus.DISPATCHING:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await registry.unsubscribe(project_id, queue)

    meta = store.read_meta()
    try:
        await _send(
            websocket,
            {
                "type": "turn_complete",
                "status": meta.status.value,
                "tokens_used": meta.tokens_used,
            },
        )
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
    except RuntimeError:
        pass


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(payload))


def _translate_event(event: Any) -> dict[str, Any] | None:
    """Translate internal StreamEvent into the wire protocol shape.

    Known agent-stream kinds get explicit shapes. Execution-loop kinds (task_start,
    phase_complete, etc.) pass through as-is with their payload so the frontend can
    render them without the translator needing to know about every one.
    """
    kind = event.kind
    p = event.payload or {}
    if kind == "text_delta":
        return {"type": "text_delta", "text": p.get("text", "")}
    if kind == "tool_use_start":
        return {"type": "tool_use", "name": p.get("name"), "input": p.get("input", {})}
    if kind == "tool_result":
        return {
            "type": "tool_result",
            "name": p.get("name"),
            "is_error": p.get("is_error", False),
            "preview": p.get("content_preview", ""),
        }
    if kind == "usage":
        return {
            "type": "usage",
            "input_tokens": p.get("input_tokens", 0),
            "output_tokens": p.get("output_tokens", 0),
        }
    if kind == "error":
        return {"type": "error", "message": p.get("message", "Unknown error")}
    if kind == "turn_complete":
        # APIRunner's end-of-turn event; drop the raw result object which isn't JSON-safe
        return None
    # Execution-loop events (task_start, scheduler_decision, phase_complete,
    # project_complete, task_escalated, deadlock, budget_exceeded, task_blocked,
    # loop_paused, loop_exit, loop_safety_halt, task_outcome).
    # Pass through with the event's kind as type, carrying the payload fields.
    # task_outcome carries a TaskOutcome dataclass — drop it, the frontend will
    # refresh state via HTTP polling and doesn't need the raw object on the wire.
    if kind == "task_outcome":
        outcome = p.get("outcome")
        return {
            "type": "task_outcome",
            "outcome_kind": getattr(outcome, "kind", None).value
            if outcome is not None and getattr(outcome, "kind", None) is not None
            else None,
            "summary": getattr(outcome, "summary", ""),
        }
    if kind == "review_outcome":
        # Reviewer emits this at the end of its run. The result is a ReviewResult
        # dataclass — serialize the wire-relevant fields. The Coder-facing effects
        # (task reset to pending with notes, or marked done) happen in the
        # execution loop, which emits its own review_approved / review_request_changes /
        # task_blocked events. So this one is mostly informational for the live UI.
        result = p.get("result")
        return {
            "type": "review_outcome",
            "result_kind": getattr(result, "kind", None),
            "summary": getattr(result, "summary", ""),
            "findings": list(getattr(result, "findings", []) or []),
        }
    # Generic passthrough: everything remaining payload field is wire-safe already.
    return {"type": kind, **{k: v for k, v in p.items() if _jsonable(v)}}


def _jsonable(v: Any) -> bool:
    """Guard against non-JSON-serializable payload fields slipping onto the wire."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, (list, tuple)):
        return all(_jsonable(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _jsonable(val) for k, val in v.items())
    return False
