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


def _build_runner(store: ProjectStore):
    """Construct the active AgentRunner based on the backend's configured runner.

    Centralized so every endpoint that needs an orchestrator uses the same selection
    logic — swapping the runner at deploy time is one env-var change. Import both
    runner modules lazily to avoid pulling in claude-agent-sdk on startup when the
    API runner is active (or vice versa).

    When runner==claude_code, we do NOT attempt to read an API key. The key may
    still be in the environment (user forgot to remove it), but we want it to be
    strictly ignored so there's no chance of accidental billing.
    """
    settings = get_settings()
    if settings.runner == "claude_code":
        from ..agents.claude_code_runner import ClaudeCodeRunner

        logger.debug("Building ClaudeCodeRunner (cwd=%s)", store.root)
        return ClaudeCodeRunner(cwd=str(store.root))
    elif settings.runner == "api":
        from ..agents.api_runner import APIRunner

        logger.debug("Building APIRunner")
        return APIRunner(api_key=get_api_key())
    else:
        raise RuntimeError(
            f"Unknown runner setting: {settings.runner!r} "
            f"(expected 'claude_code' or 'api')"
        )


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


# ------------------------------------------------------------------------------------------------
# Model catalog endpoint — single source of truth for the frontend's
# model-picker dropdowns. Returns the available models with display labels
# and cost hints, plus the current global defaults so the UI can show them
# in the "(default: ...)" option text.
# ------------------------------------------------------------------------------------------------


class ModelChoice(BaseModel):
    string: str
    label: str
    cost_hint: str


class ModelCatalogResponse(BaseModel):
    choices: list[ModelChoice]
    # Current global default for each role. UI shows these in the "(default)"
    # option of each dropdown so the user knows what they're falling back to.
    defaults: dict[str, str]


@router.get("/models/catalog", response_model=ModelCatalogResponse)
async def get_model_catalog(_api_key: str = Depends(get_api_key)) -> ModelCatalogResponse:
    """Return the catalog of valid models + current global defaults per role."""
    from ..config import AGENT_ROLES, MODEL_CHOICES

    settings = get_settings()
    return ModelCatalogResponse(
        choices=[ModelChoice(**c) for c in MODEL_CHOICES],
        defaults={role: getattr(settings, f"model_{role}") for role in AGENT_ROLES},
    )


def _save_registry(entries: list[dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


# Sentinel value the frontend sends when the user picks "(default)" in a
# model dropdown. Means: clear any per-project override and revert to the
# global default. Distinct from None (= "no change") and from a real model
# string (= "set this as the override").
_MODEL_DEFAULT_SENTINEL = "default"


def _validate_model_override(value: str | None, role: str) -> None:
    """Raise HTTPException(400) if `value` isn't a valid model spec.

    Accepts:
      - None: caller didn't include this field in their request → no change
      - "default": sentinel meaning "clear override, use global default"
      - any string in MODEL_CHOICES: a valid model override

    Anything else → 400 with the bad value and the list of valid choices in
    the error message so the user can correct it without consulting docs.
    """
    if value is None or value == _MODEL_DEFAULT_SENTINEL:
        return
    from ..config import valid_model_strings

    valid = valid_model_strings()
    if value not in valid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid model for {role}: {value!r}. "
                f"Valid choices are: {sorted(valid)}, or "
                f"{_MODEL_DEFAULT_SENTINEL!r} to clear the override."
            ),
        )


def _resolve_model(meta_value: str | None, settings_default: str) -> str:
    """Resolve effective model: per-project override OR global default."""
    return meta_value if meta_value else settings_default


def _apply_model_overrides_to_meta(meta: Any, body: Any) -> None:
    """Apply the four `model_<role>` fields from `body` onto `meta` in place.

    Three input forms per field:
      - body.model_X is None → caller didn't include this; leave meta as-is
      - body.model_X is "default" → clear override on meta (set to None)
      - body.model_X is "claude-...-...": validated; set on meta

    Validation must already have passed via _validate_model_override before
    this is called (we don't re-validate here — keep failure-mode separate).
    """
    from ..config import AGENT_ROLES

    for role in AGENT_ROLES:
        attr = f"model_{role}"
        new_value = getattr(body, attr, None)
        if new_value is None:
            # Field was omitted from request — no change.
            continue
        if new_value == _MODEL_DEFAULT_SENTINEL:
            # Explicit clear → revert to global default.
            setattr(meta, attr, None)
        else:
            setattr(meta, attr, new_value)


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
    # Per-agent model overrides. None means "use the global default from
    # config.Settings". Validation happens at endpoint time so the validator
    # can produce a 400 with the bad model name in the message — pydantic
    # field-level validators happen before the endpoint sees the request.
    model_architect: str | None = None
    model_dispatcher: str | None = None
    model_coder: str | None = None
    model_reviewer: str | None = None
    # Browser-based runtime verification toggle. Off by default — enabling
    # it requires a one-time Playwright + Chromium install (~150MB), so
    # opting in is explicit. Mid-project toggling is supported via the
    # update endpoint.
    playwright_enabled: bool = False


class ProjectSummary(BaseModel):
    id: str
    name: str
    root_path: str
    status: str
    created_at: float
    tokens_used: int
    tasks_completed: int
    # True when a background execution job is actively running for this project.
    # Distinct from `status` (which is on-disk state): a project can have status
    # EXECUTING but no running job (e.g. status was set but the job was cancelled
    # on backend restart), in which case the UI offers a Resume button.
    is_running: bool = False


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
    # Platform the project was created on ("windows" | "macos" | "linux").
    # Drives shell-syntax hints injected into Coder/Reviewer prompts.
    user_platform: str = "linux"
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
    # Resolved per-agent models. These are the resolved values — never None.
    # If the project has an override in meta.json we return that; otherwise the
    # global default from config. Frontend uses these to show the current
    # selection in the settings dropdowns.
    model_architect: str
    model_dispatcher: str
    model_coder: str
    model_reviewer: str
    # Raw per-project overrides — None when not set. Frontend uses these to
    # distinguish "user explicitly picked this model" from "this happens to
    # match the global default". When None, the dropdown should show
    # "(default)"; when set, it should show the model the user picked.
    override_model_architect: str | None = None
    override_model_dispatcher: str | None = None
    override_model_coder: str | None = None
    override_model_reviewer: str | None = None
    # Phase IDs from plan.md not yet marked done in meta.phases. Empty list
    # means the project's plan is fully executed (or there's no plan yet).
    # Frontend uses this to conditionally render "Resume phases" — visible
    # only when the project has unfinished plan phases. Helps rescue projects
    # that hit the multi-phase auto-advance bug fixed in scheduler.py.
    unresolved_phase_ids: list[str] = []
    # Browser-based runtime verification toggle. Surfaced so the UI can show
    # a status indicator and the EditProjectModal can toggle it. The Reviewer
    # reads this from meta directly at the start of each review.
    playwright_enabled: bool = False


class PlanApprovalRequest(BaseModel):
    approved: bool
    feedback: str | None = None


class TaskReviewRequest(BaseModel):
    approved: bool
    feedback: str | None = None


class UpdateProjectRequest(BaseModel):
    """Partial update to a project's settings. Any omitted field stays as-is.

    Field-level rules enforced in the endpoint:
      - `name`: free-form, 1-200 chars.
      - Budget/limit fields: positive integers; no upper cap enforced here
        (users have their own reason for large budgets).
      - `max_wall_clock_seconds`: null means "no limit". Positive int otherwise.
      - `root_path`: only editable when the project is NOT running and the new
        path already contains a .devteam/meta.json whose id matches this project.
        We don't move files on the user's behalf; they move the folder, then we
        update the pointer. This prevents accidents with open editors, OneDrive
        sync, or running processes that hold handles to the old location.
    """
    name: str | None = Field(default=None, min_length=1, max_length=200)
    root_path: str | None = None
    project_token_budget: int | None = Field(default=None, gt=0)
    default_task_token_budget: int | None = Field(default=None, gt=0)
    max_task_iterations: int | None = Field(default=None, gt=0)
    # Use a sentinel field for "set to null" vs "omit" since pydantic can't
    # distinguish those natively. The frontend sends `max_wall_clock_mode`
    # explicitly so the intent is unambiguous.
    max_wall_clock_seconds: int | None = Field(default=None, gt=0)
    clear_max_wall_clock: bool = False
    # Platform the user is on: "windows", "macos", or "linux". Affects what
    # shell-syntax hints Coder/Reviewer inject into their prompts. Default
    # when creating is auto-detected from the backend host; user can override
    # via this PATCH endpoint (e.g. the user is on macOS but wants Linux-style
    # commands for some reason).
    user_platform: str | None = Field(default=None, pattern="^(windows|macos|linux)$")
    # Per-agent model overrides. None = "no change to existing setting".
    # Empty string sentinel "default" means "clear the override and revert to
    # the global default" (UI sends this when user picks the "(default)" option).
    # Anything else must be a valid model string from MODEL_CHOICES; validated
    # in the endpoint, not via Pydantic, so we can return a 400 with details.
    model_architect: str | None = None
    model_dispatcher: str | None = None
    model_coder: str | None = None
    model_reviewer: str | None = None
    # Toggle browser-based runtime verification. None = "leave as-is";
    # True/False = explicit on/off. Independent of model overrides — user
    # might enable Playwright on a project without changing any models.
    playwright_enabled: bool | None = None


class UpdateTaskRequest(BaseModel):
    """Partial update to a single task. Only provided fields change.

    Safe to call while the task is running — the Coder reads the task fresh
    from disk on each iteration, so a new budget or note takes effect on the
    NEXT iteration. The current iteration finishes with its existing context.

    `add_note`: appends to the task's notes list rather than replacing it.
    Useful for nudging the Coder mid-run without losing prior notes.

    `interrupt`: when paired with add_note, force-surfaces the note to the user
    as a task-review moment. The task flips to 'review' status and the project
    to AWAITING_TASK_REVIEW, which halts the execution loop between iterations.
    Use this for "stop and show me X" notes that the Coder shouldn't just pick
    up passively on its next read_task. Ignored if add_note is empty.
    """
    budget_tokens: int | None = Field(default=None, gt=0)
    add_note: str | None = None
    interrupt: bool = False


class BulkBudgetRequest(BaseModel):
    """Apply a new budget to all non-done tasks in one call. Handy when the
    user realizes mid-project that defaults were too low."""
    budget_tokens: int = Field(gt=0)


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

    # Validate any per-agent model overrides BEFORE doing more work so a bad
    # request can 400 cleanly without leaving a half-initialized project.
    from ..config import AGENT_ROLES

    for role in AGENT_ROLES:
        _validate_model_override(getattr(body, f"model_{role}", None), role)

    # Apply budget overrides from the request
    settings = get_settings()
    meta.project_token_budget = body.project_token_budget or settings.default_project_token_budget
    meta.default_task_token_budget = (
        body.default_task_token_budget or settings.default_task_token_budget
    )
    meta.max_task_iterations = body.max_task_iterations or settings.default_max_task_iterations
    meta.max_wall_clock_seconds = body.max_wall_clock_seconds
    # Detect the backend host's platform so Coder/Reviewer hand the user
    # syntax-appropriate commands. Baked in at creation; sharing a codebase
    # across OSes later doesn't retrigger detection (user can edit via PATCH).
    from ..config import detect_host_platform

    meta.user_platform = detect_host_platform()
    # Per-agent model overrides (None for any role the user didn't override).
    _apply_model_overrides_to_meta(meta, body)
    # Playwright runtime-verification toggle. Defaults False so existing
    # users opt-in explicitly.
    meta.playwright_enabled = body.playwright_enabled
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
    from ..orchestrator.job_registry import get_registry

    registry = _load_registry()
    job_registry = get_registry()
    summaries: list[ProjectSummary] = []
    pruned: list[str] = []
    for entry in registry:
        # Auto-prune orphan registry entries. If the project folder was moved
        # or deleted out from under us, the registry should stop showing a
        # dead pointer. This happens quietly; the user sees the project
        # disappear from the list rather than a loud error.
        root = Path(entry["root_path"])
        if not root.exists() or not (root / ".devteam" / "meta.json").exists():
            pruned.append(entry["id"])
            logger.info(
                "Pruning orphan registry entry %s (path %s no longer has a "
                "valid .devteam/meta.json)",
                entry["id"], entry["root_path"],
            )
            continue
        try:
            store = ProjectStore(entry["root_path"])
            _recover_stuck_state(store)
            _backfill_user_platform(store)
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
                    is_running=job_registry.is_running(meta.id),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping project %s: %s", entry.get("id"), exc)
    if pruned:
        remaining = [e for e in registry if e["id"] not in pruned]
        _save_registry(remaining)
    return summaries


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: str, _api_key: str = Depends(get_api_key)) -> ProjectDetail:
    store = _load_store(project_id)
    return _to_detail(store)


@router.patch("/{project_id}", response_model=ProjectDetail)
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    _api_key: str = Depends(get_api_key),
) -> ProjectDetail:
    """Edit a project's settings. Partial update — only provided fields change.

    Safety rails:
      - root_path changes require the project to NOT be running (backend refuses
        mid-execution) and require the new path to already have a valid
        .devteam/meta.json with the same project id. We don't copy files for
        the user; they move the folder themselves then point us at it.
      - Name, budgets, and limits can be changed at any time. Budget changes
        take effect immediately on the next task scheduled; existing task
        budgets are preserved.
      - Registry is kept in sync for name and root_path (the two things it
        tracks that live outside meta.json).
    """
    from ..orchestrator.job_registry import get_registry as _get_job_registry

    registry = _load_registry()
    entry = next((e for e in registry if e["id"] == project_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    store = ProjectStore(entry["root_path"])
    meta = store.read_meta()
    registry_dirty = False

    # --- root_path change: strict validation, refuse if running ---
    if body.root_path is not None and body.root_path.strip() != entry["root_path"]:
        if _get_job_registry().is_running(project_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot change root_path while execution is running. "
                    "Pause the project or wait for it to finish."
                ),
            )
        new_root = Path(body.root_path.strip()).expanduser().resolve()
        if not new_root.exists() or not new_root.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"New root_path does not exist or is not a directory: {new_root}",
            )
        # Require the new path to already hold this project's .devteam/ state.
        # This catches typos, wrong-folder-selection, and "I forgot to move the
        # folder first" — we refuse rather than silently orphaning state.
        new_meta_path = new_root / ".devteam" / "meta.json"
        if not new_meta_path.exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"New root_path is missing .devteam/meta.json. Move the "
                    f"project folder (including .devteam/) to the new location "
                    f"first, then update the path here. Expected: {new_meta_path}"
                ),
            )
        try:
            import json as _json
            other_meta = _json.loads(new_meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"Could not read .devteam/meta.json at new path: {exc}",
            ) from exc
        if other_meta.get("id") != project_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"New path contains a different project "
                    f"(id={other_meta.get('id')!r}, expected {project_id!r}). "
                    f"Refusing to overwrite."
                ),
            )

        # Re-bind the store to the new path so subsequent writes land there.
        store = ProjectStore(new_root)
        meta = store.read_meta()
        meta.root_path = str(new_root)
        entry["root_path"] = str(new_root)
        registry_dirty = True
        logger.info(
            "Project %s root_path changed from %r to %r",
            project_id, entry["root_path"], str(new_root),
        )

    # --- name ---
    if body.name is not None and body.name.strip() != meta.name:
        meta.name = body.name.strip()
        entry["name"] = meta.name
        registry_dirty = True

    # --- numeric budgets ---
    if body.project_token_budget is not None:
        meta.project_token_budget = body.project_token_budget
    if body.default_task_token_budget is not None:
        meta.default_task_token_budget = body.default_task_token_budget
    if body.max_task_iterations is not None:
        meta.max_task_iterations = body.max_task_iterations

    # --- wall clock: either set to a positive int, or clear to None ---
    if body.clear_max_wall_clock:
        meta.max_wall_clock_seconds = None
    elif body.max_wall_clock_seconds is not None:
        meta.max_wall_clock_seconds = body.max_wall_clock_seconds

    # --- user_platform (validated via regex in the Pydantic model) ---
    if body.user_platform is not None:
        meta.user_platform = body.user_platform

    # --- per-agent model overrides ---
    # Validate first so all four are checked before any meta mutation; that
    # way a bad value in role 3 doesn't leave us with roles 1-2 already
    # changed and the rest skipped.
    from ..config import AGENT_ROLES

    for role in AGENT_ROLES:
        _validate_model_override(getattr(body, f"model_{role}", None), role)
    _apply_model_overrides_to_meta(meta, body)

    # --- playwright_enabled toggle ---
    # None = "no change", True/False = explicit flip. Reviewer reads
    # this fresh on every review, so the toggle takes effect on the
    # next review cycle without any restart.
    if body.playwright_enabled is not None:
        meta.playwright_enabled = body.playwright_enabled

    store.write_meta(meta)
    if registry_dirty:
        _save_registry(registry)
    await store.append_decision(
        {
            "actor": "user",
            "kind": "project_settings_updated",
            "fields": [
                k for k, v in {
                    "name": body.name,
                    "root_path": body.root_path,
                    "project_token_budget": body.project_token_budget,
                    "default_task_token_budget": body.default_task_token_budget,
                    "max_task_iterations": body.max_task_iterations,
                    "max_wall_clock_seconds": body.max_wall_clock_seconds,
                    "clear_max_wall_clock": body.clear_max_wall_clock or None,
                    "user_platform": body.user_platform,
                    "model_architect": body.model_architect,
                    "model_dispatcher": body.model_dispatcher,
                    "model_coder": body.model_coder,
                    "model_reviewer": body.model_reviewer,
                    "playwright_enabled": body.playwright_enabled,
                }.items() if v is not None
            ],
        }
    )
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
    """User approves or rejects the plan. Rejection requires feedback for the architect.

    On rejection, we append the user's feedback to the interview log and flip status
    back to INTERVIEW. The Architect turn itself is fired by the WebSocket route at
    /ws/architect/{id} — on connect, it detects an unanswered user message at the
    end of the interview log and runs the turn. This way the turn's events stream
    correctly to the user's WS (instead of a background task that has no subscriber),
    and the seeded message persists if the user navigates away and returns later.
    """
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    orchestrator = Orchestrator(store=store, runner=_build_runner(store))

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

    # On approval, kick off the background job so dispatcher + execution run
    # even if the user immediately navigates away. Without this, approving a
    # plan and closing the tab would leave the project stuck in DISPATCHING.
    if body.approved:
        await _ensure_background_execution(project_id, store)

    return _to_detail(store)


@router.post("/{project_id}/pause", response_model=ProjectDetail)
async def pause_project(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    orchestrator = Orchestrator(store=store, runner=_build_runner(store))
    await orchestrator.pause()
    return _to_detail(store)


@router.post("/{project_id}/resume", response_model=ProjectDetail)
async def resume_paused_project(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Resume a PAUSED project back to EXECUTING. The frontend's execution WebSocket
    re-opens on the status change and the loop resumes at the next task boundary.

    Distinct from resume_execution (which resets blocked tasks). This one is the
    companion to /pause — user clicked pause, now clicks resume."""
    from ..orchestrator import Orchestrator
    from ..state import ProjectStatus

    store = _load_store(project_id)
    meta = store.read_meta()
    if meta.status != ProjectStatus.PAUSED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume: project is {meta.status.value}, not paused.",
        )
    orchestrator = Orchestrator(store=store, runner=_build_runner(store))
    await orchestrator.resume(resume_to=ProjectStatus.EXECUTING)
    # Resuming from paused means work should continue — start the background
    # job so the user doesn't have to open the project to trigger it.
    await _ensure_background_execution(project_id, store)
    return _to_detail(store)


@router.post("/{project_id}/retry_dispatcher", response_model=ProjectDetail)
async def retry_dispatcher(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Retry the Dispatcher after a BLOCKED state. Flips status back to DISPATCHING;
    the frontend's dispatcher WebSocket auto-reopens on status change."""
    from ..orchestrator import Orchestrator

    store = _load_store(project_id)
    orchestrator = Orchestrator(store=store, runner=_build_runner(store))
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

    # Flip project back to executing, then start the background job so work
    # resumes immediately without waiting for a WS viewer to attach.
    meta.status = ProjectStatus.EXECUTING
    store.write_meta(meta)
    await store.append_decision(
        {"actor": "user", "kind": "execution_resumed", "note": "Blocked tasks reset to pending."}
    )
    await _ensure_background_execution(project_id, store)
    return _to_detail(store)


@router.delete("/{project_id}", response_model=dict[str, str])
async def delete_project(
    project_id: str,
    purge: bool = False,
    _api_key: str = Depends(get_api_key),
) -> dict[str, str]:
    """Remove a project from Dev Team's list.

    Default (purge=false): unregister only. The project folder and all its
    files stay intact on disk — the user's code is never touched. Just the
    entry in projects.json is removed and Dev Team forgets about it.

    purge=true: ALSO deletes the .devteam/ subfolder (which holds meta.json,
    plan.md, tasks.json, decisions.log, review scratch). The user's own code
    in the project directory is STILL untouched. This is for when the user
    wants to reset Dev Team state for a project without scrapping the code.

    Refuses to delete a project with a running background job — pause or let
    it finish first. This prevents a race where the job tries to write to
    disk after we've already cleared state.
    """
    from ..orchestrator.job_registry import get_registry as _get_job_registry
    import shutil

    registry = _load_registry()
    entry = next((e for e in registry if e["id"] == project_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    job_registry = _get_job_registry()
    if job_registry.is_running(project_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot delete a project with a running execution job. "
                "Pause the project or wait for it to finish, then try again."
            ),
        )

    # Purge first (if requested) — only .devteam/, never anything outside it.
    # We do this before unregistering so if purge fails, the registry entry
    # survives and the user can retry.
    if purge:
        root = Path(entry["root_path"])
        devteam_dir = root / ".devteam"
        if devteam_dir.exists():
            try:
                shutil.rmtree(devteam_dir)
                logger.info(
                    "Purged .devteam state for project %s at %s",
                    project_id, devteam_dir,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to purge .devteam folder: {exc}",
                ) from exc

    # Unregister
    remaining = [e for e in registry if e["id"] != project_id]
    _save_registry(remaining)

    # Reclaim the in-memory agent event buffer for this project. Cheap; just
    # frees the deque-backed event storage and the seq counter. If the buffer
    # is empty for this project (never recorded any events), this is a no-op.
    try:
        from ..orchestrator.agent_event_buffer import get_buffer
        get_buffer().clear(project_id)
    except Exception:  # noqa: BLE001
        # Best-effort cleanup. Buffer state is in-memory only; on next backend
        # restart it'll be empty anyway. No reason to fail the delete here.
        logger.debug("Failed to clear agent event buffer for %s", project_id, exc_info=True)

    logger.info(
        "Deleted project %s from registry (purge=%s)", project_id, purge
    )
    return {
        "id": project_id,
        "status": "deleted",
        "purged": "true" if purge else "false",
    }


@router.post("/{project_id}/add_work", response_model=ProjectDetail)
async def add_work(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Reopen a completed project for additional work.

    Transitions status from COMPLETE (or any other state the user might be in)
    back to INTERVIEW. The existing plan.md, tasks.json, and completed phases
    are preserved — the Architect is expected to read plan.md and append a new
    phase rather than rewrite anything.

    We intentionally ONLY allow this from COMPLETE and INTERVIEW (re-entry). From
    an actively running project, this would collide with execution in flight.
    """
    store = _load_store(project_id)
    meta = store.read_meta()

    allowed = {ProjectStatus.COMPLETE, ProjectStatus.INTERVIEW}
    if meta.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot add work: project is {meta.status.value}. "
                f"Add-work requires project to be complete (or already in an "
                f"add-work interview). Active projects should run to completion first."
            ),
        )

    meta.status = ProjectStatus.INTERVIEW
    # Clear current_phase so the Dispatcher picks up the newly-added phase after
    # approval. Existing phases are preserved in meta.phases (they're already
    # marked done, so approve_plan's phase parser will correctly identify the
    # new one as the only pending phase).
    meta.current_phase = None
    store.write_meta(meta)

    await store.append_decision(
        {
            "actor": "user",
            "kind": "add_work_requested",
            "note": "User reopened completed project to add additional work.",
        }
    )
    return _to_detail(store)


@router.post("/{project_id}/force_submit_plan", response_model=ProjectDetail)
async def force_submit_plan(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Bypass the Architect and submit the current plan.md for user approval.

    Backup for the "Architect is stuck in narration loops" failure mode:
    when the model keeps logging 'handing off' decision entries instead of
    actually calling write_plan + request_approval, the user can force the
    transition themselves. Requires:
      - Project in INTERVIEW state (the only state where you'd need this).
      - plan.md has non-trivial content (>20 chars of non-whitespace).

    Does NOT modify plan.md. Whatever's on disk is what gets submitted. If
    the user wants to edit plan.md before approving, they should do so
    manually in their editor first, then hit this endpoint, then the normal
    approve-plan button in the UI.
    """
    store = _load_store(project_id)
    meta = store.read_meta()

    if meta.status != ProjectStatus.INTERVIEW:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot force-submit: project is {meta.status.value}, "
                f"expected interview. Force-submit only makes sense when the "
                f"Architect is stuck mid-interview and won't call request_approval."
            ),
        )

    plan = store.read_plan()
    if len(plan.strip()) < 20:
        raise HTTPException(
            status_code=400,
            detail=(
                "plan.md is empty or too short. Write the plan first — either "
                "by letting the Architect call write_plan, or by editing "
                ".devteam/plan.md directly in your editor."
            ),
        )

    meta.status = ProjectStatus.AWAIT_APPROVAL
    store.write_meta(meta)
    await store.append_decision(
        {
            "actor": "user",
            "kind": "plan_force_submitted",
            "note": (
                "User bypassed the Architect and submitted plan.md directly for "
                "approval. Usually because the Architect got stuck in a handoff "
                "narration loop without actually calling request_approval."
            ),
        }
    )
    logger.info("Force-submitted plan for project %s", project_id)
    return _to_detail(store)


@router.patch("/{project_id}/tasks/{task_id}", response_model=dict[str, Any])
async def update_task(
    project_id: str,
    task_id: str,
    body: UpdateTaskRequest,
    _api_key: str = Depends(get_api_key),
) -> dict[str, Any]:
    """Edit a single task's budget or notes. Safe to call during execution.

    The Coder re-reads the task from disk at the start of each iteration, so
    a budget bump or appended note takes effect on the NEXT iteration. The
    in-flight iteration (if any) completes with its existing context.

    Does not touch status, dependencies, iterations count, or acceptance
    criteria — those are Dispatcher/Coder/Reviewer territory.
    """
    store = _load_store(project_id)
    tasks = store.read_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    updates: dict[str, Any] = {}
    if body.budget_tokens is not None:
        updates["budget_tokens"] = body.budget_tokens
    note_added = False
    if body.add_note is not None and body.add_note.strip():
        existing_notes = list(task.get("notes", []))
        existing_notes.append(f"User note: {body.add_note.strip()}")
        updates["notes"] = existing_notes
        note_added = True

    # Interrupt path: force the task to user-review state so the execution loop
    # halts between iterations, regardless of whether the Coder would notice
    # the note on its own. Only meaningful when there's actually a note to
    # surface; a naked interrupt with no message is useless.
    # Skip if the task is already done — no point surfacing a note on a
    # completed task; the user would just be blocked from progressing.
    interrupting = body.interrupt and note_added and task.get("status") != "done"
    if interrupting:
        updates["status"] = "review"
        # Flag used by the review endpoint to distinguish "Coder finished and
        # wants review" from "user interrupted mid-task." Approve semantics
        # differ: a Coder-flagged task approve marks it done; a user-interrupt
        # approve means "resume the Coder, don't mark done."
        updates["interrupted_by_user"] = True

    if not updates:
        return task  # no-op; return current state

    updated = store.update_task(task_id, updates)

    # If interrupting, also flip the project-level status so the execution
    # loop picks up the halt on its next iteration. The loop checks meta.status
    # between tasks; AWAITING_TASK_REVIEW short-circuits it cleanly.
    if interrupting:
        meta = store.read_meta()
        meta.status = ProjectStatus.AWAITING_TASK_REVIEW
        store.write_meta(meta)

    await store.append_decision(
        {
            "actor": "user",
            "kind": "task_interrupted" if interrupting else "task_edited",
            "task_id": task_id,
            "fields": list(updates.keys()),
        }
    )
    return updated or task


@router.post("/{project_id}/tasks/bulk_budget", response_model=dict[str, Any])
async def bulk_update_task_budget(
    project_id: str,
    body: BulkBudgetRequest,
    _api_key: str = Depends(get_api_key),
) -> dict[str, Any]:
    """Apply a new per-task token budget to every non-done task in one shot.

    Useful when you realize mid-project that defaults were too low and don't
    want to edit tasks one at a time. Skips tasks already marked 'done' since
    they have no work left to budget. Tasks that have already exceeded the
    new budget (due to prior iterations) aren't rewound — the Coder will
    just see 'remaining = new_budget - tokens_spent_so_far' on next attempt.
    """
    store = _load_store(project_id)
    tasks = store.read_tasks()
    updated_ids: list[str] = []
    for t in tasks:
        if t.get("status") == "done":
            continue
        store.update_task(t["id"], {"budget_tokens": body.budget_tokens})
        updated_ids.append(t["id"])

    await store.append_decision(
        {
            "actor": "user",
            "kind": "bulk_task_budget_updated",
            "budget_tokens": body.budget_tokens,
            "task_count": len(updated_ids),
            "task_ids": updated_ids,
        }
    )
    return {"updated": len(updated_ids), "task_ids": updated_ids}



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

        # User-interrupted tasks shouldn't be marked done on approve — the
        # Coder didn't finish them. "Approve" here semantically means "OK,
        # continue; my note has been addressed or acknowledged." Reset to
        # pending so the Coder picks it up again, and clear the interrupt
        # flag so the next review cycle (if any) works normally.
        if task.get("interrupted_by_user"):
            store.update_task(
                task_id,
                {
                    "status": "pending",
                    "interrupted_by_user": False,
                },
            )
            meta = store.read_meta()
            meta.status = ProjectStatus.EXECUTING
            store.write_meta(meta)
            await store.append_decision(
                {
                    "actor": "user",
                    "kind": "task_interrupt_resumed",
                    "task_id": task_id,
                }
            )
        else:
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
                # Clear interrupt flag if set; the task is going back into
                # normal flow with the user's feedback appended.
                "interrupted_by_user": False,
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

    # Both approve and reject branches flip to EXECUTING — the loop should
    # resume either way (to continue past the approved task, or to rerun the
    # rejected one). Start the background job.
    await _ensure_background_execution(project_id, store)
    return _to_detail(store)


# ------------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------------


def _load_store(project_id: str) -> ProjectStore:
    registry = _load_registry()
    for entry in registry:
        if entry["id"] == project_id:
            store = ProjectStore(entry["root_path"])
            _recover_stuck_state(store)
            _backfill_user_platform(store)
            return store
    raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")


def _recover_stuck_state(store: ProjectStore) -> None:
    """One-time repair of projects broken by pre-fix bugs.

    Runs on every project access. Idempotent: if nothing needs fixing, does
    nothing. Safe to call from any code path that reads a store.

    Fixes applied:

    1. **Phase left 'active' after PROJECT_COMPLETE.** Single-phase projects
       historically hit PROJECT_COMPLETE without PHASE_COMPLETE ever firing,
       so the last phase stayed 'active' in meta.phases even though status
       became 'complete'. Add-work then tried to redispatch that phase and
       collided with already-completed task ids. Fix: if project status is
       COMPLETE and any phase is still 'active' or 'pending' while all its
       tasks are done, mark that phase 'done'.

    2. **Stuck in BLOCKED/DISPATCHING after a no-op add-work.** If the user
       approved an add-work plan, current_phase was set to the wrong phase
       (the stale 'active' P1 in case #1), and the Dispatcher stopped on
       collision, the project sat in BLOCKED with current_phase still wrong.
       Fix: if in BLOCKED/DISPATCHING and current_phase points to a phase
       whose tasks are all done, advance current_phase to the next pending
       phase and flip status to DISPATCHING so the user can retry.

    Only touches state that is unambiguously wrong. Never deletes task data,
    never rewrites plan.md, never changes token totals.
    """
    try:
        meta = store.read_meta()
    except Exception:  # noqa: BLE001
        return  # Can't recover what we can't read

    tasks = store.read_tasks()
    tasks_by_phase: dict[str, list[dict]] = {}
    for t in tasks:
        tasks_by_phase.setdefault(t.get("phase", ""), []).append(t)

    def _phase_tasks_all_done(phase_id: str) -> bool:
        phase_tasks = tasks_by_phase.get(phase_id, [])
        return bool(phase_tasks) and all(
            t.get("status") == "done" for t in phase_tasks
        )

    changed = False

    # Fix 1: reconcile phase statuses with their tasks. Only flip upward
    # (pending/active → done); never downgrade a done phase.
    for p in meta.phases:
        if p.status != "done" and _phase_tasks_all_done(p.id):
            logger.info(
                "Recovering project %s: marking phase %s as done "
                "(all tasks complete but status was %s)",
                meta.id, p.id, p.status,
            )
            p.status = "done"
            changed = True

    # Fix 2: if stuck in BLOCKED or DISPATCHING pointing at a done phase,
    # advance current_phase. Only advance — don't rewind or jump past pending
    # phases that haven't started.
    if meta.status in (ProjectStatus.BLOCKED, ProjectStatus.DISPATCHING):
        current = next(
            (p for p in meta.phases if p.id == meta.current_phase), None
        )
        if current is not None and current.status == "done":
            # Find the first non-done phase after the current one in list order
            next_pending = next(
                (p for p in meta.phases if p.status != "done"), None
            )
            if next_pending is not None:
                logger.info(
                    "Recovering project %s: advancing current_phase from %s "
                    "(done) to %s; setting status to dispatching",
                    meta.id, meta.current_phase, next_pending.id,
                )
                meta.current_phase = next_pending.id
                next_pending.status = "active"
                next_pending.approved_by_user = True
                meta.status = ProjectStatus.DISPATCHING
                changed = True
            else:
                # All phases done but status still BLOCKED/DISPATCHING — flip
                # to COMPLETE so the user can see it's actually finished.
                logger.info(
                    "Recovering project %s: all phases done, flipping status "
                    "to complete", meta.id,
                )
                meta.status = ProjectStatus.COMPLETE
                meta.current_phase = None
                changed = True

    if changed:
        store.write_meta(meta)


def _backfill_user_platform(store: ProjectStore) -> None:
    """If a project's meta.json predates the user_platform field, set it to the
    current host's platform on first access.

    Only backfills when the field is LITERALLY MISSING from the file — not when
    it's present but equal to the dataclass default. This prevents a real Linux
    user from being falsely re-stamped as Windows just because they happen to
    access their project from a Windows backend one day.
    """
    try:
        raw = json.loads(store.meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return

    if "user_platform" in raw:
        return  # already set; leave it alone

    from ..config import detect_host_platform

    meta = store.read_meta()
    meta.user_platform = detect_host_platform()
    store.write_meta(meta)
    logger.info(
        "Backfilled user_platform=%s for project %s", meta.user_platform, meta.id
    )


async def _ensure_background_execution(project_id: str, store: ProjectStore) -> None:
    """Start the execution loop as a background job for this project.

    Safe to call from any endpoint after flipping status to EXECUTING. The
    JobRegistry is idempotent — if a job is already running for this project,
    this is a no-op. The job runs decoupled from any WebSocket, so closing
    the UI doesn't stop execution.

    Requires an API key set in the key store when runner=api; in claude_code
    mode the key is not needed (auth comes from the user's `claude` CLI config).
    """
    from ..orchestrator import Orchestrator
    from ..orchestrator.job_registry import get_registry
    from .session import _store as _key_store

    settings = get_settings()
    if settings.runner == "api":
        api_key = _key_store.get()
        if api_key is None:
            logger.warning(
                "Cannot start background execution for %s: no API key set", project_id
            )
            return

    orchestrator = Orchestrator(store=store, runner=_build_runner(store))
    registry = get_registry()
    await registry.ensure_running(
        project_id, lambda: orchestrator.stream_execution_loop()
    )


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
    # $/million-tokens, base rates (uncached input). Verified against Anthropic
    # pricing docs April 2026. If these drift, update here — this is the only
    # place cost math references them.
    PRICES = {
        "opus": (5.0, 25.0),    # Opus 4.7 / 4.6 / 4.5 all at same rate
        "sonnet": (3.0, 15.0),  # Sonnet 4.6
        "haiku": (1.0, 5.0),    # Haiku 4.5
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
    # Need settings for model resolution: meta override → global default.
    settings = get_settings()
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
        user_platform=meta.user_platform,
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
        # Resolved per-agent models — meta override OR global default. The UI
        # uses these to populate dropdowns; never None.
        model_architect=_resolve_model(meta.model_architect, settings.model_architect),
        model_dispatcher=_resolve_model(meta.model_dispatcher, settings.model_dispatcher),
        model_coder=_resolve_model(meta.model_coder, settings.model_coder),
        model_reviewer=_resolve_model(meta.model_reviewer, settings.model_reviewer),
        # Raw overrides — None means "no per-project override set". Frontend
        # uses this to know whether to show "(default)" or a specific model
        # in the dropdown.
        override_model_architect=meta.model_architect,
        override_model_dispatcher=meta.model_dispatcher,
        override_model_coder=meta.model_coder,
        override_model_reviewer=meta.model_reviewer,
        # Compute unresolved phases by parsing plan.md and removing those
        # marked done in meta.phases. Wrapped in try/except because plan.md
        # may not exist on freshly-created projects, and a parse error here
        # shouldn't break the entire detail endpoint — fall through to empty.
        unresolved_phase_ids=_compute_unresolved_phase_ids(store, meta),
        playwright_enabled=meta.playwright_enabled,
    )


def _compute_unresolved_phase_ids(store: ProjectStore, meta: Any) -> list[str]:
    """Return phase IDs from plan.md that haven't been marked done in meta.

    Used to feed ProjectDetail.unresolved_phase_ids. Returns empty list on
    any failure (no plan.md, parse error, etc.) — the field is informational
    and shouldn't propagate exceptions.
    """
    try:
        from ..orchestrator.phases import parse_phases as _parse_phases

        plan_text = store.read_plan()
        if not plan_text.strip():
            return []
        plan_phases = _parse_phases(plan_text)
        done_ids = {p.id for p in meta.phases if p.status == "done"}
        return [pp.id for pp in plan_phases if pp.id not in done_ids]
    except Exception:  # noqa: BLE001
        # Plan missing, malformed, or store error — return empty so the
        # frontend just doesn't show the resume button. Logged at debug
        # because old projects without plans are normal during recovery
        # flows and we don't want noise.
        logger.debug(
            "Could not compute unresolved phases for %s",
            meta.id,
            exc_info=True,
        )
        return []


# ------------------------------------------------------------------------------------------------
# "Open in..." endpoints — quick launchers for the project root in Explorer,
# VS Code, or a terminal. Saves the user from manually CDing into the project
# folder every time they want to inspect output, run code, or git stuff.
#
# These shell out to OS-specific commands (Explorer/Finder/etc., `code`,
# `start cmd` on Windows). Fire-and-forget — we don't wait for the launched
# process. If the target tool isn't installed (e.g., user doesn't have VS Code),
# the launch silently no-ops at the OS level; we surface that as a 404-style
# error to keep the UI responsive without spinning forever.
# ------------------------------------------------------------------------------------------------


class OpenInRequest(BaseModel):
    target: str = Field(
        ..., pattern="^(explorer|vscode|terminal)$",
        description="Where to open the project: explorer | vscode | terminal",
    )


class OpenInResponse(BaseModel):
    ok: bool
    target: str
    path: str
    message: str


@router.post("/{project_id}/open", response_model=OpenInResponse)
async def open_project_in(
    project_id: str,
    body: OpenInRequest,
    _api_key: str = Depends(get_api_key),
) -> OpenInResponse:
    """Open the project's root directory in the user's chosen tool.

    Three targets:
      - explorer: native file manager (Windows Explorer / macOS Finder / xdg-open)
      - vscode:   VS Code via the `code` CLI (must be on PATH)
      - terminal: a fresh terminal already CDed into the project root

    All launches are fire-and-forget. We don't capture the launched process or
    wait for it to exit — the user opens it, uses it, closes it on their own
    schedule. Errors here are limited to "tool not found" (e.g., `code` not on
    PATH) which we surface so the user knows what to install.

    Cross-platform: Windows is the primary target, but macOS/Linux fallbacks
    are provided since our launcher scripts are PowerShell-only and someone
    running the backend directly on macOS/Linux should still get reasonable
    behavior.
    """
    import platform
    import shutil
    import subprocess

    store = _load_store(project_id)
    root_path = str(store.root)
    target = body.target
    system = platform.system()  # 'Windows', 'Darwin', 'Linux'

    try:
        if target == "explorer":
            if system == "Windows":
                # Use os.startfile — Windows-native, opens Explorer at the path.
                # subprocess with explorer.exe also works but startfile is simpler.
                import os as _os
                _os.startfile(root_path)  # type: ignore[attr-defined]
                msg = "Opened in Windows Explorer."
            elif system == "Darwin":
                subprocess.Popen(["open", root_path])
                msg = "Opened in Finder."
            else:
                subprocess.Popen(["xdg-open", root_path])
                msg = "Opened in file manager."

        elif target == "vscode":
            # Look for `code` on PATH. On Windows this is normally
            # %LocalAppData%\Programs\Microsoft VS Code\bin\code (added by the
            # installer's "Add to PATH" option). If missing, surface a clear
            # error rather than silently failing.
            code_cmd = shutil.which("code")
            if code_cmd is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "VS Code's `code` command isn't on your PATH. Install "
                        "VS Code, then in VS Code: Cmd/Ctrl+Shift+P → 'Shell "
                        "Command: Install code command in PATH'."
                    ),
                )
            # Use shell=False; pass args as list. On Windows, Popen with a list
            # works fine for the `code` CLI.
            subprocess.Popen([code_cmd, root_path])
            msg = "Opened in VS Code."

        elif target == "terminal":
            if system == "Windows":
                # `start cmd /K cd /D "<path>"` — opens a new cmd window, CDs
                # into the project, and stays open (/K) so the user has a shell.
                # /D handles drive changes (path may be on a different drive
                # than the current one). Use shell=True since `start` is a
                # cmd.exe builtin, not a standalone executable.
                subprocess.Popen(
                    f'start "" cmd /K cd /D "{root_path}"',
                    shell=True,
                )
                msg = "Opened a terminal in the project folder."
            elif system == "Darwin":
                # Open Terminal.app at the path. AppleScript would give more
                # control but `open -a Terminal` is good enough.
                subprocess.Popen(["open", "-a", "Terminal", root_path])
                msg = "Opened in Terminal.app."
            else:
                # Try common Linux terminals in order; if none found, fail loud.
                for term in ("gnome-terminal", "konsole", "xterm"):
                    if shutil.which(term):
                        if term == "gnome-terminal":
                            subprocess.Popen([term, "--working-directory", root_path])
                        elif term == "konsole":
                            subprocess.Popen([term, "--workdir", root_path])
                        else:  # xterm
                            subprocess.Popen([term, "-e", f"cd {root_path} && bash"])
                        break
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="No supported terminal emulator found.",
                    )
                msg = f"Opened in terminal."

        else:
            # Defensive — pattern validator should have caught this.
            raise HTTPException(status_code=400, detail=f"Unknown target: {target}")

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Open-in failed: target=%s path=%s", target, root_path)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to open in {target}: {exc}",
        ) from exc

    logger.info("Opened project %s in %s (%s)", project_id, target, root_path)
    return OpenInResponse(ok=True, target=target, path=root_path, message=msg)


# ------------------------------------------------------------------------------------------------
# Resume-phases endpoint — rescue projects that hit the now-fixed multi-phase
# auto-advance bug. Symptom: project is marked COMPLETE but plan.md has
# undone phases (P2, P3, etc.) that the Dispatcher never decomposed.
#
# Why this exists separately from the regular advance flow:
# The bug was that the scheduler emitted PROJECT_COMPLETE the moment all of
# P1's tasks finished, because tasks.json only contained P1 tasks (Dispatcher
# only decomposes one phase at a time). The fix to scheduler.py prevents new
# projects from hitting this, but existing stuck projects sit at status=COMPLETE
# with meta.phases marked done up through whatever ran. They need a manual
# kick to resume.
#
# What this does NOT do:
#  - Does not call the Architect. The plan is already written; we just need
#    to dispatch the next phase that was never reached.
#  - Does not modify plan.md. The plan stands as-is.
#  - Does not silently fix anything on backend startup. User-triggered only.
# ------------------------------------------------------------------------------------------------


class ResumePhasesResponse(BaseModel):
    ok: bool
    resumed_phase_id: str
    message: str


@router.post("/{project_id}/resume_phases", response_model=ProjectDetail)
async def resume_phases(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> ProjectDetail:
    """Resume a project that has undone phases in plan.md but is sitting at
    status=COMPLETE (or any non-running status). Sets current_phase to the
    next undone phase and flips status to DISPATCHING so the Dispatcher
    decomposes that phase on next run.

    Refuses if:
      - The project has no undone phases in plan.md (genuinely complete).
      - Plan.md cannot be parsed.
      - The project is currently running (DISPATCHING/EXECUTING/etc.) — those
        states mean a worker may be operating on it. We don't want to race.
      - tasks.json already has tasks for the phase we'd resume — that means
        a prior partial dispatch left state we shouldn't silently overwrite.
        User can investigate by opening tasks.json and deleting/editing those
        rows, then retrying.
    """
    store = _load_store(project_id)
    meta = store.read_meta()

    # Refuse if a worker may currently be operating on this project. The
    # frontend should normally only show the button on completed projects,
    # but defense-in-depth — a user could send the request directly.
    running_states = {
        ProjectStatus.DISPATCHING,
        ProjectStatus.EXECUTING,
        ProjectStatus.AWAITING_TASK_REVIEW,
        ProjectStatus.PHASE_REVIEW,
    }
    if meta.status in running_states:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Project is currently {meta.status.value!r}. Wait for it to "
                f"settle (or pause it) before resuming phases."
            ),
        )

    # Parse plan.md to find phases the Architect wrote.
    try:
        from ..orchestrator.phases import parse_phases as _parse_phases

        plan_text = store.read_plan()
        plan_phases = _parse_phases(plan_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse plan.md for resume")
        raise HTTPException(
            status_code=400,
            detail=f"Could not parse plan.md: {exc}",
        ) from exc

    if not plan_phases:
        raise HTTPException(
            status_code=400,
            detail=(
                "plan.md has no phases — nothing to resume. If you want to add "
                "new work, use 'Add work' instead."
            ),
        )

    # Find the first phase in plan.md that isn't marked done in meta.phases.
    # We trust meta.phases as the authority for what's actually been finished
    # because that's what the execution loop updates as phases complete.
    done_phase_ids = {p.id for p in meta.phases if p.status == "done"}
    next_phase_id: str | None = None
    for pp in plan_phases:
        if pp.id not in done_phase_ids:
            next_phase_id = pp.id
            break

    if next_phase_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "All phases in plan.md are already marked done. Nothing to "
                "resume. If you want to add new work, use 'Add work' instead."
            ),
        )

    # Defensive: if tasks.json already has tasks for the phase we'd resume,
    # something is off — a prior partial dispatch may have left state we
    # shouldn't silently overwrite. Refuse and surface the conflict so the
    # user can decide.
    existing_tasks = store.read_tasks()
    pre_existing_phase_tasks = [
        t for t in existing_tasks if t.get("phase") == next_phase_id
    ]
    if pre_existing_phase_tasks:
        ids = ", ".join(t["id"] for t in pre_existing_phase_tasks[:5])
        more = (
            f" (and {len(pre_existing_phase_tasks) - 5} more)"
            if len(pre_existing_phase_tasks) > 5
            else ""
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot auto-resume {next_phase_id}: tasks.json already has "
                f"{len(pre_existing_phase_tasks)} task(s) for that phase "
                f"({ids}{more}). Inspect tasks.json and either remove those "
                f"rows or use a different recovery path."
            ),
        )

    # Ensure meta.phases has an entry for the phase being resumed. Older
    # projects' meta may have stopped listing phases past the one that ran.
    if not any(p.id == next_phase_id for p in meta.phases):
        # Find the phase title from plan.md so the UI shows something
        # readable instead of just "P2".
        title = next(
            (p.title for p in plan_phases if p.id == next_phase_id),
            next_phase_id,
        )
        from ..state import ProjectPhase

        meta.phases.append(
            ProjectPhase(id=next_phase_id, title=title, status="active")
        )
    else:
        # Mark the resumed phase as active (if it was pending or was
        # somehow marked done despite never running).
        for p in meta.phases:
            if p.id == next_phase_id:
                p.status = "active"

    meta.current_phase = next_phase_id
    meta.status = ProjectStatus.DISPATCHING
    store.write_meta(meta)

    await store.append_decision(
        {
            "actor": "orchestrator",
            "kind": "phases_resumed",
            "from_status": "complete",
            "phase_id": next_phase_id,
            "reason": "User clicked Resume phases on a project with undone phases in plan.md",
        }
    )
    logger.info("Resumed project %s at phase %s", project_id, next_phase_id)
    return _to_detail(store)


# ------------------------------------------------------------------------------------------------
# Agent event buffer endpoints — feed the frontend's Agent Inspector panel.
#
# The buffer captures every StreamEvent flowing through the orchestrator,
# tagged by which agent (Architect / Dispatcher / Coder / Reviewer / orchestrator)
# produced it. Frontend polls these endpoints to render unified per-agent
# transcripts. See app/orchestrator/agent_event_buffer.py for the data model.
# ------------------------------------------------------------------------------------------------


class AgentEventEntry(BaseModel):
    agent: str
    kind: str
    payload: dict[str, Any]
    timestamp: float
    seq: int
    task_id: str | None = None


class AgentEventsResponse(BaseModel):
    events: list[AgentEventEntry]
    latest_seq: int


@router.get("/{project_id}/agent_events", response_model=AgentEventsResponse)
async def get_agent_events(
    project_id: str,
    agent: str | None = None,
    since: int = 0,
    _api_key: str = Depends(get_api_key),
) -> AgentEventsResponse:
    """Fetch buffered agent events for the project.

    Query params:
      - agent (optional): restrict to one role: architect | dispatcher | coder
        | reviewer | orchestrator. Omit for all agents merged in seq order.
      - since (optional, default 0): return only events with seq > since.
        Frontend tracks the highest seq seen and polls with `since=<that>`
        to get tail-only updates without re-fetching the whole buffer.

    The response includes `latest_seq` so the frontend can use it as the
    next `since` value without scanning the events list. Cheaper than
    `max(e.seq for e in events)` when there are many events or none.
    """
    # Validate project exists (404 if not — same shape as other endpoints)
    _load_store(project_id)

    from ..orchestrator.agent_event_buffer import get_buffer

    buf = get_buffer()
    events = buf.fetch(project_id, agent=agent, since=since)
    return AgentEventsResponse(
        events=[AgentEventEntry(**e) for e in events],
        latest_seq=buf.latest_seq(project_id),
    )


class AgentSummaryEntry(BaseModel):
    event_count: int
    last_seq: int
    last_ts: float
    last_kind: str | None


class AgentSummaryResponse(BaseModel):
    # Map of agent role to summary. Frontend uses this to show event counts
    # and activity indicators on the tab strip without fetching all events.
    agents: dict[str, AgentSummaryEntry]
    latest_seq: int


@router.get("/{project_id}/agent_events/summary", response_model=AgentSummaryResponse)
async def get_agent_events_summary(
    project_id: str, _api_key: str = Depends(get_api_key)
) -> AgentSummaryResponse:
    """Per-agent activity summary for the inspector tab strip.

    Cheap to call — returns counts and last-event metadata without the events
    themselves. Frontend can poll this on a faster cadence (every 1s) than
    the full events endpoint to update tab indicators.
    """
    _load_store(project_id)
    from ..orchestrator.agent_event_buffer import get_buffer

    buf = get_buffer()
    summary = buf.agent_summary(project_id)
    return AgentSummaryResponse(
        agents={k: AgentSummaryEntry(**v) for k, v in summary.items()},
        latest_seq=buf.latest_seq(project_id),
    )
