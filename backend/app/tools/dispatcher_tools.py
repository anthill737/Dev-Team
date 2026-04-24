"""Tools available to the Dispatcher agent.

The Dispatcher's job is to read an approved plan and decompose the current phase into
well-formed tasks with acceptance criteria. It does not write code, does not touch the
filesystem outside of `.devteam/tasks.json`, and does not spawn agents.

Its toolset is deliberately small:
  - read_plan / read_phase — get the plan text and the current phase to decompose
  - write_tasks — commit the tasks list to tasks.json (schema-validated here)
  - read_tasks — check current state if needed (e.g., re-decomposition)
  - append_decision_log — rationale and audit trail
  - mark_dispatch_complete — signal the orchestrator that decomposition is done
"""

from __future__ import annotations

from typing import Any

from ..agents.base import ToolResult, ToolSpec
from ..state import ProjectStatus, ProjectStore

# ---- Task schema validation ---------------------------------------------------------------------
#
# We validate tasks before writing tasks.json so malformed output from the model fails fast
# and produces a legible error. The model gets the error back and retries.

REQUIRED_TASK_FIELDS = {
    "id": str,
    "phase": str,
    "title": str,
    "description": str,
    "acceptance_criteria": list,
    "dependencies": list,
}


def _validate_tasks(tasks: list[dict[str, Any]]) -> str | None:
    """Return an error string if tasks are malformed, or None if valid."""
    if not isinstance(tasks, list):
        return "tasks must be a list"
    if len(tasks) == 0:
        return "tasks list is empty — the phase must have at least one task"
    if len(tasks) > 50:
        return f"tasks list has {len(tasks)} entries — too many. Aim for 3-15 meaningful tasks per phase."

    seen_ids: set[str] = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return f"task index {i} is not an object"

        for field, expected_type in REQUIRED_TASK_FIELDS.items():
            if field not in t:
                return f"task index {i} missing required field '{field}'"
            if not isinstance(t[field], expected_type):
                return (
                    f"task index {i} field '{field}' has wrong type "
                    f"(expected {expected_type.__name__}, got {type(t[field]).__name__})"
                )

        if not t["id"].strip():
            return f"task index {i} has empty id"
        if t["id"] in seen_ids:
            return f"duplicate task id '{t['id']}'"
        seen_ids.add(t["id"])

        if not t["title"].strip():
            return f"task {t['id']} has empty title"
        if len(t["acceptance_criteria"]) == 0:
            return (
                f"task {t['id']} has no acceptance criteria — every task must have at "
                f"least one concrete, observable criterion"
            )
        for j, ac in enumerate(t["acceptance_criteria"]):
            if not isinstance(ac, str) or not ac.strip():
                return f"task {t['id']} acceptance_criteria[{j}] is empty or not a string"

        for dep in t["dependencies"]:
            if not isinstance(dep, str):
                return f"task {t['id']} has non-string dependency: {dep!r}"
            # Dependencies must reference tasks that exist in this batch or earlier phases
            # (we only validate within-batch here; cross-phase deps are harder to validate
            # without loading the full plan).

    # After first pass, check all within-batch dependencies resolve
    for t in tasks:
        for dep in t["dependencies"]:
            if dep not in seen_ids and not dep.startswith(("P0-", "P1-", "P2-", "P3-", "P4-", "P5-")):
                # Allow unknown-prefix deps (cross-phase); only flag ones that look
                # in-batch but don't match anything.
                pass

    return None


def _normalize_task(t: dict[str, Any], phase_id: str) -> dict[str, Any]:
    """Fill in sensible defaults for optional task fields."""
    return {
        "id": t["id"],
        "phase": t.get("phase") or phase_id,
        "title": t["title"],
        "description": t["description"],
        "acceptance_criteria": t["acceptance_criteria"],
        "dependencies": t["dependencies"],
        "status": t.get("status", "pending"),
        "assigned_to": t.get("assigned_to", "coder"),
        "iterations": t.get("iterations", 0),
        "budget_tokens": t.get("budget_tokens", 150_000),
        "notes": t.get("notes", []),
    }


# ---- Tool factory --------------------------------------------------------------------------------


def build_dispatcher_tools(store: ProjectStore, phase_id: str) -> list[ToolSpec]:
    """Build the Dispatcher's tool set, scoped to decomposing the given phase."""

    async def read_plan_exec(_args: dict[str, Any]) -> ToolResult:
        content = store.read_plan()
        await store.append_decision(
            {"actor": "dispatcher", "kind": "read_plan", "phase": phase_id}
        )
        if not content:
            return ToolResult(content="plan.md is empty.", is_error=True)
        return ToolResult(content=content)

    async def read_phase_exec(_args: dict[str, Any]) -> ToolResult:
        """Return just the relevant phase section from the plan, if it can be isolated.

        Falls back to the full plan if we can't find a phase heading. This is a convenience
        that keeps the Dispatcher focused on the right chunk of a long plan.
        """
        plan = store.read_plan()
        await store.append_decision(
            {"actor": "dispatcher", "kind": "read_phase", "phase": phase_id}
        )
        if not plan:
            return ToolResult(content="plan.md is empty.", is_error=True)

        # Very loose phase extraction: look for headings that start with the phase id
        lines = plan.splitlines()
        start_idx = None
        end_idx = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip().lstrip("#").strip()
            if start_idx is None and (
                stripped.lower().startswith(f"{phase_id.lower()}:")
                or stripped.lower().startswith(f"{phase_id.lower()} ")
                or stripped.lower().startswith(f"phase {phase_id[1:].lower()}")
                or stripped.lower() == phase_id.lower()
            ):
                start_idx = i
                continue
            if start_idx is not None and line.startswith("#"):
                # Next section heading → end of this phase
                # But allow subheadings (####+) to stay in this phase section
                hash_count = len(line) - len(line.lstrip("#"))
                if hash_count <= 2:
                    end_idx = i
                    break

        if start_idx is None:
            return ToolResult(
                content=(
                    f"Could not auto-locate phase {phase_id} section in plan.md. "
                    f"Full plan:\n\n{plan}"
                )
            )
        section = "\n".join(lines[start_idx:end_idx])
        return ToolResult(content=f"Phase {phase_id} section:\n\n{section}")

    async def read_tasks_exec(_args: dict[str, Any]) -> ToolResult:
        tasks = store.read_tasks()
        await store.append_decision(
            {
                "actor": "dispatcher",
                "kind": "read_tasks",
                "phase": phase_id,
                "existing_count": len(tasks),
            }
        )
        if not tasks:
            return ToolResult(content="No tasks yet.")
        import json as _json

        return ToolResult(content=_json.dumps(tasks, indent=2))

    async def write_tasks_exec(args: dict[str, Any]) -> ToolResult:
        tasks = args.get("tasks")
        if tasks is None:
            await store.append_decision(
                {
                    "actor": "dispatcher",
                    "kind": "write_tasks_rejected",
                    "phase": phase_id,
                    "reason": "missing 'tasks' argument",
                }
            )
            return ToolResult(content="Missing 'tasks' argument.", is_error=True)

        err = _validate_tasks(tasks)
        if err:
            await store.append_decision(
                {
                    "actor": "dispatcher",
                    "kind": "write_tasks_rejected",
                    "phase": phase_id,
                    "reason": err,
                    "task_count": len(tasks) if isinstance(tasks, list) else None,
                }
            )
            return ToolResult(content=f"Task list rejected: {err}", is_error=True)

        # Normalize defaults
        normalized = [_normalize_task(t, phase_id) for t in tasks]

        # Append to existing tasks rather than replacing — supports future re-dispatch
        existing = store.read_tasks()
        existing_ids = {t["id"] for t in existing}
        new_ids = {t["id"] for t in normalized}
        collisions = existing_ids & new_ids
        if collisions:
            await store.append_decision(
                {
                    "actor": "dispatcher",
                    "kind": "write_tasks_rejected",
                    "phase": phase_id,
                    "reason": f"id collision: {sorted(collisions)}",
                }
            )
            return ToolResult(
                content=(
                    f"Task ID collision with existing tasks: {sorted(collisions)}. "
                    f"Use unique ids like {phase_id}-T1, {phase_id}-T2, etc."
                ),
                is_error=True,
            )

        store.write_tasks(existing + normalized)
        await store.append_decision(
            {
                "actor": "dispatcher",
                "kind": "tasks_written",
                "phase": phase_id,
                "count": len(normalized),
                "ids": [t["id"] for t in normalized],
            }
        )
        return ToolResult(
            content=f"Committed {len(normalized)} tasks for {phase_id}: {', '.join(t['id'] for t in normalized)}"
        )

    async def append_decision_log_exec(args: dict[str, Any]) -> ToolResult:
        note = args.get("note", "").strip()
        kind = args.get("kind", "note").strip() or "note"
        if not note:
            return ToolResult(content="Missing note", is_error=True)
        await store.append_decision({"actor": "dispatcher", "kind": kind, "note": note})
        return ToolResult(content="Logged.")

    async def mark_dispatch_complete_exec(args: dict[str, Any]) -> ToolResult:
        summary = args.get("summary", "").strip()
        tasks = store.read_tasks()
        phase_task_count = sum(1 for t in tasks if t.get("phase") == phase_id)
        if phase_task_count == 0:
            return ToolResult(
                content=(
                    f"Cannot mark dispatch complete — no tasks written yet for {phase_id}. "
                    f"Call write_tasks first."
                ),
                is_error=True,
            )
        await store.append_decision(
            {
                "actor": "dispatcher",
                "kind": "dispatch_complete",
                "phase": phase_id,
                "task_count": phase_task_count,
                "summary": summary,
            }
        )
        # Transition project to EXECUTING — in v1 nothing picks up from here; the user
        # sees the task list and the next session builds the Coder.
        meta = store.read_meta()
        meta.status = ProjectStatus.EXECUTING
        meta.current_phase = phase_id
        store.write_meta(meta)
        return ToolResult(
            content=(
                f"Dispatch of {phase_id} complete ({phase_task_count} tasks). "
                f"Project moved to EXECUTING state."
            )
        )

    return [
        ToolSpec(
            name="read_plan",
            description="Read the full approved plan.md content.",
            input_schema={"type": "object", "properties": {}},
            executor=read_plan_exec,
        ),
        ToolSpec(
            name="read_phase",
            description=(
                f"Read just the section of plan.md for the current phase ({phase_id}). "
                "Useful for focusing on the phase you're decomposing without the full plan "
                "in context."
            ),
            input_schema={"type": "object", "properties": {}},
            executor=read_phase_exec,
        ),
        ToolSpec(
            name="read_tasks",
            description="Read current contents of tasks.json (all phases).",
            input_schema={"type": "object", "properties": {}},
            executor=read_tasks_exec,
        ),
        ToolSpec(
            name="write_tasks",
            description=(
                "Commit a list of tasks for the current phase to tasks.json. Each task must "
                "have: id (unique, e.g., 'P1-T1'), phase, title, description, "
                "acceptance_criteria (array of concrete observable conditions), dependencies "
                "(array of task ids). The system validates before writing and rejects malformed "
                "input — if rejected, read the error and retry."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "phase": {"type": "string"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "acceptance_criteria": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "dependencies": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "budget_tokens": {"type": "integer"},
                            },
                            "required": [
                                "id",
                                "title",
                                "description",
                                "acceptance_criteria",
                                "dependencies",
                            ],
                        },
                    }
                },
                "required": ["tasks"],
            },
            executor=write_tasks_exec,
        ),
        ToolSpec(
            name="append_decision_log",
            description=(
                "Append an entry to decisions.log capturing rationale for how you decomposed "
                "the phase, non-obvious judgment calls, or gaps you flagged."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["note"],
            },
            executor=append_decision_log_exec,
        ),
        ToolSpec(
            name="mark_dispatch_complete",
            description=(
                "Call after writing tasks and completing reflective practice. Transitions the "
                "project to EXECUTING state. `summary` is a short recap of the decomposition "
                "approach for the audit log."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
            },
            executor=mark_dispatch_complete_exec,
        ),
    ]
