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

from ..agents.api_runner import APIRunner
from ..orchestrator import Orchestrator
from ..state import ProjectStore
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
    orchestrator = Orchestrator(store=store, runner=APIRunner(api_key=api_key))

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
    """Multi-turn conversation loop — client sends user_message, server streams response."""
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
        if not user_text:
            await _send(websocket, {"type": "error", "message": "Empty user message"})
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
    """Stream the execution loop (Coder driving tasks) until a terminal state."""
    try:
        async for event in orchestrator.stream_execution_loop():
            out = _translate_event(event)
            if out is not None:
                await _send(websocket, out)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Execution loop failed for project %s", project_id)
        await _send(
            websocket,
            {"type": "error", "message": f"Execution loop failed: {exc}"},
        )

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


async def _dispatcher_oneshot(
    websocket: WebSocket, orchestrator: Orchestrator, store: ProjectStore, project_id: str
) -> None:
    """One-shot: run the dispatcher once, stream events, close."""
    try:
        async for event in orchestrator.stream_dispatcher_turn():
            out = _translate_event(event)
            if out is not None:
                await _send(websocket, out)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dispatcher run failed for project %s", project_id)
        await _send(
            websocket,
            {"type": "error", "message": f"Dispatcher run failed: {exc}"},
        )

    meta = store.read_meta()
    await _send(
        websocket,
        {
            "type": "turn_complete",
            "status": meta.status.value,
            "tokens_used": meta.tokens_used,
        },
    )
    # Dispatcher is one-shot — close the socket now so the frontend knows it's done.
    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)


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
