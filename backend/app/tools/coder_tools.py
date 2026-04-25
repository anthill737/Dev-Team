"""Tools available to the Coder agent.

The Coder writes code, runs tests, and iterates until a task passes acceptance criteria
or escalates. Its tools are scoped through the sandbox (no arbitrary filesystem or shell
access) and through the ProjectStore (for reading task spec and logging decisions).

Tool list:
  - read_task         — read the current task's spec + acceptance criteria +
                        relevant prior decisions (rework notes, failed bash, etc.)
  - read_plan         — read the full plan for context (tech stack, conventions)
  - read_decisions    — fetch more prior decisions than read_task auto-includes,
                        optionally filtered by kind
  - fs_list           — list a directory inside the project
  - fs_read           — read a text file inside the project
  - fs_write          — write or replace a text file inside the project (creates parents)
  - bash              — run a whitelisted command in the sandbox
  - append_decision_log — explain a non-obvious decision for the audit trail
  - signal_outcome    — declare this task done (approved/needs_rework/blocked). Ends the run.
"""

from __future__ import annotations

from typing import Any, Callable

from ..agents.base import ToolResult, ToolSpec
from ..orchestrator.task_runner import TaskOutcome, TaskOutcomeKind
from ..sandbox import (
    CommandDenied,
    PathOutsideProject,
    SandboxExecutor,
    safe_list,
    safe_read,
    safe_write,
)
from ..state import ProjectStore


# Caps for tool output fed back to the model. Keep individual tool responses under
# ~15KB so the model's context doesn't get dominated by tool output from one call.
_DEFAULT_FILE_READ_BYTES = 80_000
_DEFAULT_LIST_ENTRIES = 200
_BASH_DEFAULT_TIMEOUT = 60
_BASH_MAX_TIMEOUT = 300

# Decisions filtering — the log contains every file_write and bash call, so we curate
# what gets surfaced back to the Coder.
#
# AUTO-INCLUDE in read_task (when iteration > 1): high-signal entries about THIS task
# only. Keeps context lean; the Coder sees what happened without having to ask.
_TASK_RELEVANT_DECISION_KINDS = frozenset(
    {
        "task_rework",
        "task_needs_user_review",
        "task_user_rejected",
        "task_user_approved",
        "task_blocked",
        "task_failed",
        "task_approved",
        "note",  # Coder's own append_decision_log entries
    }
)

# Failed bash calls are the other thing the Coder most needs to see on retry —
# a command that failed last time is probably the thing to think about differently.
# We include these only when they're from the same task.
_MAX_AUTO_DECISIONS_IN_READ_TASK = 15
_MAX_DECISIONS_IN_READ_DECISIONS = 50


def build_coder_tools(
    store: ProjectStore,
    sandbox: SandboxExecutor,
    task: dict[str, Any],
    *,
    outcome_receiver: Callable[[TaskOutcome], None],
) -> list[ToolSpec]:
    """Build the Coder's tool set for a single task run.

    `outcome_receiver` is a callback the signal_outcome tool calls when the Coder declares
    the task done. The agent's run loop watches this receiver — as soon as it's called,
    the Coder's turn ends.
    """

    # ---- Task + plan context --------------------------------------------------

    async def read_task_exec(_args: dict[str, Any]) -> ToolResult:
        import json as _json

        # Re-read from disk each call rather than using the captured `task`.
        # This is essential for mid-task user edits: if the user adds a note or
        # bumps the budget via the UI while we're running, we want the NEXT
        # read_task call to surface it. The closure-captured `task` is a
        # snapshot from task start, so it won't reflect PATCHes.
        all_tasks = store.read_tasks()
        fresh = next((t for t in all_tasks if t["id"] == task["id"]), None)
        if fresh is None:
            # Extremely unlikely, but don't crash — fall back to the snapshot.
            fresh = task

        iteration = fresh.get("iterations", 1)
        all_notes = fresh.get("notes", [])

        # Split user notes out of the general notes list so the Coder can't
        # accidentally treat them as historical bookkeeping. Any note that
        # starts with "User note:" was inserted by the user via the UI's gear
        # edit; the rest come from the Coder's own iteration history.
        # The prompt instructs the Coder to treat user_notes as BINDING
        # instructions on par with acceptance criteria — not commentary to
        # skim past.
        user_notes = [n for n in all_notes if isinstance(n, str) and n.startswith("User note:")]
        other_notes = [n for n in all_notes if not (isinstance(n, str) and n.startswith("User note:"))]

        view: dict[str, Any] = {
            "id": fresh["id"],
            "phase": fresh["phase"],
            "title": fresh["title"],
            "description": fresh["description"],
            "acceptance_criteria": fresh["acceptance_criteria"],
            "dependencies": fresh.get("dependencies", []),
            "iteration": iteration,
            # Put user_notes ABOVE prior_notes in the JSON output so the Coder
            # sees them first. Only include the key if non-empty so we don't
            # train the model to expect an "empty list means no user input."
        }
        if user_notes:
            view["user_notes"] = user_notes
        view["prior_notes"] = other_notes

        # On retries, auto-include recent relevant decisions for THIS task so the
        # Coder doesn't have to explicitly fetch history to know why the last
        # attempt failed. Iteration 1 skips this — adding history where there is
        # none just wastes tokens and confuses the model.
        if iteration > 1:
            history = _relevant_task_decisions(
                store=store,
                task_id=fresh["id"],
                limit=_MAX_AUTO_DECISIONS_IN_READ_TASK,
            )
            if history:
                view["relevant_history"] = history

        return ToolResult(content=_json.dumps(view, indent=2))

    async def read_plan_exec(_args: dict[str, Any]) -> ToolResult:
        content = store.read_plan()
        if not content:
            return ToolResult(content="plan.md is empty.", is_error=True)
        return ToolResult(content=content)

    async def read_decisions_exec(args: dict[str, Any]) -> ToolResult:
        import json as _json

        scope = (args.get("scope") or "this_task").strip().lower()
        max_entries = int(args.get("limit", 20))
        max_entries = max(1, min(_MAX_DECISIONS_IN_READ_DECISIONS, max_entries))

        kinds_filter = args.get("kinds")
        if kinds_filter is not None and not isinstance(kinds_filter, list):
            return ToolResult(
                content="'kinds' must be a list of strings if provided.",
                is_error=True,
            )
        kinds_set = (
            frozenset(k for k in kinds_filter if isinstance(k, str))
            if kinds_filter
            else None
        )

        if scope == "this_task":
            entries = _relevant_task_decisions(
                store=store,
                task_id=task["id"],
                limit=max_entries,
                include_all_kinds=True,
                kinds_filter=kinds_set,
            )
            header = f"Recent decisions for task {task['id']}:"
        elif scope == "all":
            # Pull a window from the tail of the log, filter by kinds if given.
            raw = store.read_decisions(limit=200)
            if kinds_set is not None:
                raw = [d for d in raw if d.get("kind") in kinds_set]
            entries = raw[-max_entries:]
            header = "Recent decisions across the whole project:"
        else:
            return ToolResult(
                content=(
                    f"Invalid scope {scope!r}. Use 'this_task' (default) or 'all'."
                ),
                is_error=True,
            )

        if not entries:
            return ToolResult(content=f"{header}\n  (no matching entries)")
        return ToolResult(content=f"{header}\n{_json.dumps(entries, indent=2)}")

    # ---- Filesystem -----------------------------------------------------------

    async def fs_list_exec(args: dict[str, Any]) -> ToolResult:
        path = args.get("path", ".")
        max_entries = int(args.get("max_entries", _DEFAULT_LIST_ENTRIES))
        include_hidden = bool(args.get("include_hidden", False))
        try:
            entries, truncated = safe_list(
                sandbox.project_root,
                path,
                max_entries=max_entries,
                include_hidden=include_hidden,
            )
        except FileNotFoundError as exc:
            return ToolResult(content=str(exc), is_error=True)
        except NotADirectoryError as exc:
            return ToolResult(content=str(exc), is_error=True)
        except PathOutsideProject as exc:
            return ToolResult(content=str(exc), is_error=True)

        lines = []
        for e in entries:
            marker = "/" if e.is_dir else ""
            size = "" if e.is_dir else f"  ({e.size_bytes} bytes)"
            lines.append(f"  {e.relative_path}{marker}{size}")
        suffix = f"\n[truncated at {max_entries} entries]" if truncated else ""
        header = f"Contents of {path or '.'}:"
        body = "\n".join(lines) if lines else "  (empty)"
        return ToolResult(content=f"{header}\n{body}{suffix}")

    async def fs_read_exec(args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        if not path:
            return ToolResult(content="Missing 'path'", is_error=True)
        max_bytes = int(args.get("max_bytes", _DEFAULT_FILE_READ_BYTES))
        try:
            result = safe_read(sandbox.project_root, path, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            return ToolResult(content=str(exc), is_error=True)
        except IsADirectoryError as exc:
            return ToolResult(content=str(exc), is_error=True)
        except PathOutsideProject as exc:
            return ToolResult(content=str(exc), is_error=True)

        return ToolResult(content=result.content)

    async def fs_write_exec(args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        content = args.get("content")
        if not path:
            return ToolResult(content="Missing 'path'", is_error=True)
        if content is None:
            return ToolResult(content="Missing 'content'", is_error=True)
        if not isinstance(content, str):
            return ToolResult(
                content=f"'content' must be a string, got {type(content).__name__}",
                is_error=True,
            )
        try:
            resolved = safe_write(sandbox.project_root, path, content)
        except PathOutsideProject as exc:
            return ToolResult(content=str(exc), is_error=True)
        except OSError as exc:
            return ToolResult(content=f"Write failed: {exc}", is_error=True)

        # Log the write to decisions for user visibility
        try:
            rel = resolved.relative_to(sandbox.project_root)
            rel_str = str(rel).replace("\\", "/")
        except ValueError:
            rel_str = str(resolved)
        await store.append_decision(
            {
                "actor": "coder",
                "kind": "file_written",
                "task_id": task["id"],
                "path": rel_str,
                "bytes": len(content.encode("utf-8")),
            }
        )
        return ToolResult(content=f"Wrote {len(content)} chars to {rel_str}")

    # ---- Bash -----------------------------------------------------------------

    async def bash_exec(args: dict[str, Any]) -> ToolResult:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv:
            return ToolResult(
                content=(
                    "Missing or empty 'argv'. This tool takes a list of arguments, not a "
                    "shell string — e.g., ['pytest', '-q']. No shell interpretation."
                ),
                is_error=True,
            )
        if not all(isinstance(a, str) for a in argv):
            return ToolResult(
                content="All argv entries must be strings", is_error=True
            )
        timeout_seconds = min(
            _BASH_MAX_TIMEOUT,
            max(1, int(args.get("timeout_seconds", _BASH_DEFAULT_TIMEOUT))),
        )

        try:
            result = await sandbox.run(argv, timeout_seconds=timeout_seconds)
        except CommandDenied as exc:
            return ToolResult(content=f"Command denied: {exc}", is_error=True)

        # Log the bash call — especially useful when tests run
        await store.append_decision(
            {
                "actor": "coder",
                "kind": "bash",
                "task_id": task["id"],
                "argv": argv,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
            }
        )

        # Format a rich response so the model can diagnose failures
        status_line = (
            f"timed out after {timeout_seconds}s"
            if result.timed_out
            else f"exit={result.exit_code}"
        )
        parts = [f"$ {' '.join(argv)}  [{status_line}, {result.duration_ms}ms]"]
        if result.stdout:
            parts.append("--- stdout ---")
            parts.append(result.stdout)
        if result.stderr:
            parts.append("--- stderr ---")
            parts.append(result.stderr)
        text = "\n".join(parts)

        # Report as error so the model's fix-the-test loop notices
        is_error = result.timed_out or (result.exit_code is not None and result.exit_code != 0)
        return ToolResult(content=text, is_error=is_error)

    # ---- Logging + outcome ----------------------------------------------------

    async def append_decision_log_exec(args: dict[str, Any]) -> ToolResult:
        note = (args.get("note") or "").strip()
        kind = (args.get("kind") or "note").strip() or "note"
        if not note:
            return ToolResult(content="Missing note", is_error=True)
        await store.append_decision(
            {"actor": "coder", "kind": kind, "task_id": task["id"], "note": note}
        )
        return ToolResult(content="Logged.")

    async def signal_outcome_exec(args: dict[str, Any]) -> ToolResult:
        status = (args.get("status") or "").strip().lower()
        summary = (args.get("summary") or "").strip()
        if status == "approved":
            if not summary:
                return ToolResult(
                    content=(
                        "'summary' is required when status=approved — briefly state "
                        "what was built and how acceptance criteria were met."
                    ),
                    is_error=True,
                )
            outcome_receiver(
                TaskOutcome(kind=TaskOutcomeKind.APPROVED, summary=summary)
            )
            return ToolResult(content="Outcome recorded: APPROVED. Stop working now.")
        if status == "needs_rework":
            notes = (args.get("rework_notes") or "").strip()
            if not notes:
                return ToolResult(
                    content="'rework_notes' required when status=needs_rework",
                    is_error=True,
                )
            outcome_receiver(
                TaskOutcome(
                    kind=TaskOutcomeKind.NEEDS_REWORK,
                    rework_notes=notes,
                    summary=summary,
                )
            )
            return ToolResult(content="Outcome recorded: NEEDS_REWORK. Stop working now.")
        if status == "needs_user_review":
            if not summary:
                return ToolResult(
                    content=(
                        "'summary' is required when status=needs_user_review — "
                        "briefly state what you built."
                    ),
                    is_error=True,
                )
            checklist_raw = args.get("review_checklist", [])
            if not isinstance(checklist_raw, list) or not checklist_raw:
                return ToolResult(
                    content=(
                        "'review_checklist' must be a non-empty list of strings. "
                        "Each item is a step the user should perform to verify the "
                        "task (e.g., 'Open index.html in a browser', "
                        "'Confirm canvas is visible and black')."
                    ),
                    is_error=True,
                )
            if not all(isinstance(x, str) and x.strip() for x in checklist_raw):
                return ToolResult(
                    content="All review_checklist items must be non-empty strings.",
                    is_error=True,
                )
            run_command = (args.get("review_run_command") or "").strip()
            if not run_command:
                return ToolResult(
                    content=(
                        "'review_run_command' is required when status=needs_user_review. "
                        "Tell the user exactly how to run/view what you built — a shell "
                        "command they can copy and paste, or 'Open <file> in a browser'. "
                        "The point is they shouldn't have to figure this out themselves."
                    ),
                    is_error=True,
                )
            files_raw = args.get("review_files_to_check", [])
            files_to_check: list[str] = []
            if isinstance(files_raw, list):
                files_to_check = [str(f) for f in files_raw if isinstance(f, str) and f.strip()]
            outcome_receiver(
                TaskOutcome(
                    kind=TaskOutcomeKind.NEEDS_USER_REVIEW,
                    summary=summary,
                    review_checklist=[s.strip() for s in checklist_raw],
                    review_run_command=run_command,
                    review_files_to_check=files_to_check,
                )
            )
            return ToolResult(
                content="Outcome recorded: NEEDS_USER_REVIEW. Stop working now."
            )
        if status == "blocked":
            reason = (args.get("block_reason") or "").strip()
            if not reason:
                return ToolResult(
                    content="'block_reason' required when status=blocked",
                    is_error=True,
                )
            outcome_receiver(
                TaskOutcome(
                    kind=TaskOutcomeKind.BLOCKED,
                    block_reason=reason,
                    summary=summary,
                )
            )
            return ToolResult(content="Outcome recorded: BLOCKED. Stop working now.")
        return ToolResult(
            content=(
                f"Invalid status {status!r}. Must be 'approved', 'needs_rework', "
                f"'needs_user_review', or 'blocked'."
            ),
            is_error=True,
        )

    return [
        ToolSpec(
            name="read_task",
            description=(
                "Read the current task you are working on: id, title, description, "
                "acceptance criteria, prior iterations' notes. On iteration 2+, also "
                "includes 'relevant_history' — curated decisions from prior attempts at "
                "this same task so you can see what was tried, what failed, and any "
                "rework/review feedback. Call this first."
            ),
            input_schema={"type": "object", "properties": {}},
            executor=read_task_exec,
        ),
        ToolSpec(
            name="read_plan",
            description=(
                "Read the project's plan.md — tech stack, conventions, phase context. "
                "Useful for understanding broader context your task fits into."
            ),
            input_schema={"type": "object", "properties": {}},
            executor=read_plan_exec,
        ),
        ToolSpec(
            name="read_decisions",
            description=(
                "Fetch more prior decisions than read_task's auto-included history. "
                "Default scope is 'this_task' (decisions tagged with your task id). "
                "Use scope='all' for project-wide decisions (Architect research, "
                "Dispatcher choices, cross-task patterns). Optionally filter by `kinds` "
                "— a list of decision kinds like ['bash', 'file_written', 'task_rework']."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["this_task", "all"],
                        "description": "Which decisions to fetch. Default 'this_task'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (1-50, default 20).",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional filter. Examples: 'bash' to see previous bash "
                            "calls, 'task_rework' / 'task_user_rejected' to see what "
                            "reviewers have said, 'file_written' to see what's been "
                            "written."
                        ),
                    },
                },
            },
            executor=read_decisions_exec,
        ),
        ToolSpec(
            name="fs_list",
            description=(
                "List files in a directory inside the project (relative to project root). "
                "Use this to explore what exists before reading or writing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to project root. Default '.'.",
                    },
                    "max_entries": {"type": "integer"},
                    "include_hidden": {"type": "boolean"},
                },
            },
            executor=fs_list_exec,
        ),
        ToolSpec(
            name="fs_read",
            description=(
                "Read a text file inside the project. Returns up to max_bytes "
                "(default 80KB). For larger files, read in chunks or narrow your scope."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["path"],
            },
            executor=fs_read_exec,
        ),
        ToolSpec(
            name="fs_write",
            description=(
                "Write or replace a text file inside the project. Creates parent "
                "directories as needed. Overwrites existing files completely — read "
                "first if you need to preserve existing content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            executor=fs_write_exec,
        ),
        ToolSpec(
            name="bash",
            description=(
                "Run a command in the sandbox. IMPORTANT: this is NOT a shell — pass "
                "argv as a list of strings. No pipes, redirects, command substitution, "
                "or chaining. Example: {'argv': ['pytest', '-q', 'tests/test_foo.py']}. "
                "Only whitelisted commands are allowed (python, node, npm, pytest, "
                "vitest, git [read-only], ls, cat, grep, rg, etc.). Default timeout 60s."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command and args as a list",
                    },
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["argv"],
            },
            executor=bash_exec,
        ),
        ToolSpec(
            name="append_decision_log",
            description=(
                "Record a non-obvious decision for the audit trail — library choice, "
                "workaround rationale, deviation from the plan, flagged concern."
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
            name="signal_outcome",
            description=(
                "End the task with an outcome. REQUIRED: call this exactly once when "
                "you're done.\n\n"
                "  status='approved' — for backend logic, APIs, data processing, "
                "pure functions, CLI tools, infrastructure — anything verifiable by "
                "running a command and checking output. The Reviewer agent runs after "
                "you and verifies acceptance criteria using shell, file reads, and "
                "HTTP — they're the safety net for command-checkable work. "
                "Provide `summary`.\n\n"
                "  status='needs_user_review' — REQUIRED when the task involves a "
                "browser-rendered page, canvas/WebGL output, game UI, desktop GUI, "
                "CSS/layout work, or anything where the acceptance criterion is "
                "essentially 'open it and see if it looks right.' The Reviewer agent "
                "CANNOT open a browser or see a screen — tests passing on the JS "
                "layer does not prove the rendered output works (a Three.js scene "
                "can have green tests and ship a black screen). Don't let passing "
                "tests fool you into approving render-dependent work. "
                "Provide `summary`, `review_checklist` (concrete user steps like "
                "'open dist/index.html, click Start, verify the cockpit appears'), "
                "and `review_run_command` (exact copy-paste launch command).\n\n"
                "  status='needs_rework' — you found a gap in your own work and want "
                "another pass. Provide `rework_notes`.\n\n"
                "  status='blocked' — cannot proceed as specified; the plan or task is "
                "wrong. Provide `block_reason`.\n\n"
                "After calling, STOP. Do not make more tool calls."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "approved",
                            "needs_user_review",
                            "needs_rework",
                            "blocked",
                        ],
                    },
                    "summary": {"type": "string"},
                    "rework_notes": {"type": "string"},
                    "block_reason": {"type": "string"},
                    "review_checklist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For needs_user_review: ordered steps the user should "
                            "perform to verify the task is done."
                        ),
                    },
                    "review_run_command": {
                        "type": "string",
                        "description": (
                            "For needs_user_review: exact command the user can copy "
                            "and paste to run/view what you built. Examples: "
                            "'python -m http.server 8000 then open http://localhost:8000' "
                            "or 'node dist/app.js' or 'Open ./index.html in Chrome'."
                        ),
                    },
                    "review_files_to_check": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For needs_user_review: key file paths the user may want "
                            "to open to understand what was built."
                        ),
                    },
                },
                "required": ["status"],
            },
            executor=signal_outcome_exec,
        ),
    ]


# ------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------


def _relevant_task_decisions(
    *,
    store: ProjectStore,
    task_id: str,
    limit: int,
    include_all_kinds: bool = False,
    kinds_filter: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter the decisions log for entries relevant to a specific task.

    Default behavior (read_task auto-include): only surface task-level lifecycle
    events and the Coder's own notes, plus any failed bash calls on this task. That
    gives the model "what's happened to this task so far" without drowning it in
    every file_written event.

    When include_all_kinds=True (the explicit read_decisions tool), we relax the
    filter: any decision tagged with this task_id is fair game, optionally narrowed
    by kinds_filter.
    """
    # Read a wide window and filter — the log is append-only so this is cheap.
    # 500 entries is enough for realistic task histories without being unbounded.
    raw = store.read_decisions(limit=500)
    matching: list[dict[str, Any]] = []
    for entry in raw:
        if entry.get("task_id") != task_id:
            continue
        kind = entry.get("kind")
        if kinds_filter is not None:
            if kind not in kinds_filter:
                continue
        elif not include_all_kinds:
            # Default auto-include path: lifecycle events + failed bash + coder notes
            is_relevant_kind = kind in _TASK_RELEVANT_DECISION_KINDS
            is_failed_bash = (
                kind == "bash"
                and (
                    entry.get("exit_code") not in (0, None)
                    or entry.get("timed_out") is True
                )
            )
            if not (is_relevant_kind or is_failed_bash):
                continue
        matching.append(entry)
    # Return the most recent N
    return matching[-limit:]
