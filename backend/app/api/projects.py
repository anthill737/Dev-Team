"""Projects routes: create, list, read state, approve/reject plans."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..state import ProjectStatus, ProjectStore
from .session import get_api_key  # noqa: F401 — enforce auth

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------------------------------------
# Project registry — a single JSON file tracking which project directories the app knows about.
# This is NOT state about a specific project — that lives in each project's .devteam/.
# ------------------------------------------------------------------------------------------------


def _registry_path() -> Path:
    return Path(get_settings().projects_registry_path)


def _load_registry() -> list[dict[str, Any]]:
    path = _registry_path()
    # One-time migration: earlier builds stored the registry under the user home
    # directory. If we find one there and none at the new location, move it.
    if not path.exists():
        legacy = Path.home() / ".devteam" / "projects.json"
        if legacy.exists():
            try:
                entries = json.loads(legacy.read_text(encoding="utf-8"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
                logger.info("Migrated projects registry from %s to %s", legacy, path)
            except Exception:
                logger.exception("Failed to migrate legacy registry; starting empty")
                return []
        else:
            return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_registry(entries: list[dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


# ------------------------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    root_path: str = Field(..., description="Absolute path to the project directory")
    # Budgets — all optional, defaults from config
    project_token_budget: int | None = None
    default_task_token_budget: int | None = None
    max_task_iterations: int | None = None
    max_wall_clock_seconds: int | None = None


class ProjectSummary(BaseModel):
    id: str
    name: str
    root_path: str
    status: str
    created_at: float
    tokens_used: int
    tasks_completed: int


class ProjectDetail(BaseModel):
    id: str
    name: str
    root_path: str
    status: str
    created_at: float
    tokens_used: int
    tasks_completed: int
    project_token_budget: int
    default_task_token_budget: int
    max_task_iterations: int
    max_wall_clock_seconds: int | None
    current_phase: str | None
    phases: list[dict[str, Any]]
    # Per-model token tracking for cost display
    tokens_input_opus: int = 0
    tokens_output_opus: int = 0
    tokens_input_sonnet: int = 0
    tokens_output_sonnet: int = 0
    tokens_input_haiku: int = 0
    tokens_output_haiku: int = 0
    # Cache breakdown (subset of tokens_input_*). cache_read = hits (10% price),
    # cache_creation = writes (125% price). Lets the UI show cache effectiveness.
    cache_read_opus: int = 0
    cache_creation_opus: int = 0
    cache_read_sonnet: int = 0
    cache_creation_sonnet: int = 0
    cache_read_haiku: int = 0
    cache_creation_haiku: int = 0
    # Computed: estimated USD cost based on public Anthropic pricing. Not billed
    # against your account; this is a client-side estimate derived from usage totals.
    cost_usd_estimate: float = 0.0


class PlanApprovalRequest(BaseModel):
    approved: bool
    feedback: str | None = None


class TaskReviewRequest(BaseModel):
    approved: bool
    feedback: str | None = None


# ------------------------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------------------------


@router.post("", response_model=ProjectDetail)
async def create_project(
    body: CreateProjectRequest, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Create a new project pointed at the given directory."""
    root = Path(body.root_path).expanduser().resolve()
    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not create project directory: {exc}"
            ) from exc
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {root}")

    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    store = ProjectStore(root)
    meta = store.init(project_id=project_id, name=body.name)

    # Apply budget overrides from the request
    settings = get_settings()
    meta.project_token_budget = body.project_token_budget or settings.default_project_token_budget
    meta.default_task_token_budget = (
        body.default_task_token_budget or settings.default_task_token_budget
    )
    meta.max_task_iterations = body.max_task_iterations or settings.default_max_task_iterations
    meta.max_wall_clock_seconds = body.max_wall_clock_seconds
    store.write_meta(meta)

    # Register the project so we can list it later
    registry = _load_registry()
    registry.append(
        {"id": project_id, "name": body.name, "root_path": str(root), "created_at": time.time()}
    )
    _save_registry(registry)

    logger.info("Created project %s at %s", project_id, root)
    return _to_detail(store)


@router.get("", response_model=list[ProjectSummary])
async def list_projects(_api_key: str = Depends(get_api_key)) -> list[ProjectSummary]:
    registry = _load_registry()
    summaries: list[ProjectSummary] = []
    for entry in registry:
        try:
            store = ProjectStore(entry["root_path"])
            meta = store.read_meta()
            summaries.append(
                ProjectSummary(
                    id=meta.id,
                    name=meta.name,
                    root_path=meta.root_path,
                    status=meta.status.value,
                    created_at=meta.created_at,
                    tokens_used=meta.tokens_used,
                    tasks_completed=meta.tasks_completed,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping project %s: %s", entry.get("id"), exc)
    return summaries


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: str, _api_key: str = Depends(get_api_key)) -> ProjectDetail:
    store = _load_store(project_id)
    return _to_detail(store)


@router.get("/{project_id}/plan", response_model=dict[str, str])
async def get_plan(project_id: str, _api_key: str = Depends(get_api_key)) -> dict[str, str]:
    store = _load_store(project_id)
    return {"content": store.read_plan()}


@router.get("/{project_id}/interview", response_model=list[dict[str, Any]])
async def get_interview(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> list[dict[str, Any]]:
    store = _load_store(project_id)
    return store.read_interview()


@router.get("/{project_id}/tasks", response_model=list[dict[str, Any]])
async def get_tasks(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> list[dict[str, Any]]:
    store = _load_store(project_id)
    return store.read_tasks()


@router.get("/{project_id}/decisions", response_model=list[dict[str, Any]])
async def get_decisions(
    project_id: str, limit: int = 100, _api_key: str = Depends(get_api_key)
) -> list[dict[str, Any]]:
    store = _load_store(project_id)
    return store.read_decisions(limit=limit)


@router.post("/{project_id}/plan/decision", response_model=ProjectDetail)
async def decide_plan(
    project_id: str, body: PlanApprovalRequest, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """User approves or rejects the plan. Rejection requires feedback for the architect."""
    from ..agents.api_runner import APIRunner
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    api_key = get_api_key()  # re-fetch for orchestrator
    orchestrator = Orchestrator(store=store, runner=APIRunner(api_key=api_key))

    try:
        if body.approved:
            await orchestrator.approve_plan()
        else:
            if not body.feedback or not body.feedback.strip():
                raise HTTPException(
                    status_code=400, detail="Rejection requires non-empty feedback."
                )
            await orchestrator.reject_plan(body.feedback)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _to_detail(store)


@router.post("/{project_id}/pause", response_model=ProjectDetail)
async def pause_project(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    from ..agents.api_runner import APIRunner
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    orchestrator = Orchestrator(store=store, runner=APIRunner(api_key=get_api_key()))
    await orchestrator.pause()
    return _to_detail(store)


@router.post("/{project_id}/retry_dispatcher", response_model=ProjectDetail)
async def retry_dispatcher(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Retry the Dispatcher after a BLOCKED state. Flips status back to DISPATCHING;
    the frontend's dispatcher WebSocket auto-reopens on status change."""
    from ..agents.api_runner import APIRunner
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    orchestrator = Orchestrator(store=store, runner=APIRunner(api_key=get_api_key()))
    try:
        await orchestrator.retry_dispatcher()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_detail(store)


@router.post("/{project_id}/resume_execution", response_model=ProjectDetail)
async def resume_execution(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Resume after a task-level block (Coder exhausted budget, iteration cap, etc.).

    Resets any blocked tasks back to pending with iterations=0 and flips project
    status to EXECUTING so the loop picks them up again. The user is effectively
    saying "give this another shot" — costs more API spend but often works when
    the first attempt was derailed by something transient (bad npx path, flaky
    network, etc.)."""
    store = _load_store(project_id)
    meta = store.read_meta()
    if meta.status != ProjectStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume: project is {meta.status.value}, not blocked.",
        )

    # Reset blocked tasks to pending. iterations=0 gives them a full fresh budget
    # on the next attempt instead of continuing from wherever they gave up.
    tasks = store.read_tasks()
    resumed_any = False
    for task in tasks:
        if task.get("status") == "blocked":
            store.update_task(
                task["id"],
                {
                    "status": "pending",
                    "iterations": 0,
                    "notes": task.get("notes", [])
                    + [f"Resumed by user at {time.time()}"],
                },
            )
            resumed_any = True

    if not resumed_any:
        raise HTTPException(
            status_code=409,
            detail="Project is blocked but no tasks are in blocked state. "
            "This is probably a dispatcher block — use Retry Dispatcher instead.",
        )

    # Flip project back to executing. The frontend's execution WebSocket will
    # auto-reconnect on status change and drive the loop forward.
    meta.status = ProjectStatus.EXECUTING
    store.write_meta(meta)
    await store.append_decision(
        {"actor": "user", "kind": "execution_resumed", "note": "Blocked tasks reset to pending."}
    )
    return _to_detail(store)


@router.post("/{project_id}/tasks/{task_id}/review", response_model=ProjectDetail)
async def review_task(
    project_id: str,
    task_id: str,
    body: TaskReviewRequest,
    _api_key: str = Depends(get_api_key),
) -> ProjectDetail:
    """User approves or rejects a task that's in user-review state.

    Approve → task becomes done, project returns to EXECUTING so the loop can pick up
    the next task. Reject → task goes back to pending with the user's feedback in its
    notes, project returns to EXECUTING for the Coder to try again.
    """
    store = _load_store(project_id)
    meta = store.read_meta()
    if meta.status != ProjectStatus.AWAITING_TASK_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot review task — project is in {meta.status.value}, "
                f"expected awaiting_task_review."
            ),
        )

    tasks = store.read_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    if task.get("status") != "review":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Task {task_id} is not in review state (status={task.get('status')})."
            ),
        )

    if body.approved:
        import time as _time

        store.update_task(
            task_id,
            {
                "status": "done",
                "summary": task.get("review_summary", ""),
                "completed_at": _time.time(),
            },
        )
        meta = store.read_meta()
        meta.tasks_completed += 1
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)
        await store.append_decision(
            {
                "actor": "user",
                "kind": "task_user_approved",
                "task_id": task_id,
            }
        )
    else:
        feedback = (body.feedback or "").strip()
        if not feedback:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Rejecting a task in review requires feedback — tell the Coder "
                    "what to change."
                ),
            )
        existing_notes = list(task.get("notes", []))
        existing_notes.append(f"User review rejected: {feedback}")
        store.update_task(
            task_id,
            {
                "status": "pending",
                "notes": existing_notes,
                # Clear the review fields so we don't confuse the UI on next review
                "review_summary": None,
                "review_checklist": None,
                "review_run_command": None,
                "review_files_to_check": None,
            },
        )
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        store.write_meta(meta)
        await store.append_decision(
            {
                "actor": "user",
                "kind": "task_user_rejected",
                "task_id": task_id,
                "feedback": feedback,
            }
        )

    return _to_detail(store)


# ------------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------------


def _load_store(project_id: str) -> ProjectStore:
    registry = _load_registry()
    for entry in registry:
        if entry["id"] == project_id:
            return ProjectStore(entry["root_path"])
    raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")


def _estimate_cost_usd(meta: Any) -> float:
    """Estimate USD cost from per-model token usage with cache breakdown.

    Pricing source: Anthropic public pricing as of this build. Prices shift over
    time; if the user sees a discrepancy with their actual bill, these constants
    are the place to update. Numbers are $/million-tokens.

    Cache pricing:
      - Cache read:     10% of base input price
      - Cache creation: 125% of base input price (one-time surcharge)
      - Uncached input: 100% of base input price
    We track cache_read and cache_creation as subsets of tokens_input, so the
    uncached portion is tokens_input - cache_read - cache_creation.
    """
    # $/million-tokens, base rates (uncached input)
    PRICES = {
        "opus": (15.0, 75.0),
        "sonnet": (3.0, 15.0),
        "haiku": (0.80, 4.0),
    }
    CACHE_READ_MULT = 0.10
    CACHE_WRITE_MULT = 1.25

    total = 0.0
    for family, (in_price, out_price) in PRICES.items():
        tin = getattr(meta, f"tokens_input_{family}", 0) or 0
        tout = getattr(meta, f"tokens_output_{family}", 0) or 0
        cread = getattr(meta, f"cache_read_{family}", 0) or 0
        ccreate = getattr(meta, f"cache_creation_{family}", 0) or 0
        uncached = max(0, tin - cread - ccreate)
        total += (
            uncached * in_price
            + cread * in_price * CACHE_READ_MULT
            + ccreate * in_price * CACHE_WRITE_MULT
            + tout * out_price
        )
    return round(total / 1_000_000.0, 4)


def _to_detail(store: ProjectStore) -> ProjectDetail:
    meta = store.read_meta()
    return ProjectDetail(
        id=meta.id,
        name=meta.name,
        root_path=meta.root_path,
        status=meta.status.value,
        created_at=meta.created_at,
        tokens_used=meta.tokens_used,
        tasks_completed=meta.tasks_completed,
        project_token_budget=meta.project_token_budget,
        default_task_token_budget=meta.default_task_token_budget,
        max_task_iterations=meta.max_task_iterations,
        max_wall_clock_seconds=meta.max_wall_clock_seconds,
        current_phase=meta.current_phase,
        phases=[{"id": p.id, "title": p.title, "status": p.status, "approved_by_user": p.approved_by_user} for p in meta.phases],
        tokens_input_opus=meta.tokens_input_opus,
        tokens_output_opus=meta.tokens_output_opus,
        tokens_input_sonnet=meta.tokens_input_sonnet,
        tokens_output_sonnet=meta.tokens_output_sonnet,
        tokens_input_haiku=meta.tokens_input_haiku,
        tokens_output_haiku=meta.tokens_output_haiku,
        cache_read_opus=meta.cache_read_opus,
        cache_creation_opus=meta.cache_creation_opus,
        cache_read_sonnet=meta.cache_read_sonnet,
        cache_creation_sonnet=meta.cache_creation_sonnet,
        cache_read_haiku=meta.cache_read_haiku,
        cache_creation_haiku=meta.cache_creation_haiku,
        cost_usd_estimate=_estimate_cost_usd(meta),
    )
