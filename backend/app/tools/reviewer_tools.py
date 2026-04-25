"""Tools available to the Reviewer agent.

The Reviewer verifies the Coder's work against task acceptance criteria. It's a
skeptical quality gate — reads files, runs commands, and decides approve or
request_changes.

The Reviewer CANNOT modify the Coder's code or project files. Its only write
access is to a scratch directory under `.devteam/review-scratch/<task_id>/` where
it can drop verification scripts. This prevents the Reviewer from "fixing" what
it's supposed to be judging, while still giving it a way to write multi-line
Python scripts it needs to execute (on Windows, `python -c` mangles newlines, so
the Reviewer MUST write a .py file and then run it).

Tool list:
  - read_task                  — read the current task spec + acceptance criteria
  - fs_list                    — list files in the project (read-only)
  - fs_read                    — read a text file (read-only)
  - write_verification_script  — write a script under .devteam/review-scratch/
  - bash                       — run a whitelisted command
  - submit_review              — declare approve or request_changes. Ends the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..agents.base import ToolResult, ToolSpec
from ..sandbox import (
    CommandDenied,
    PathOutsideProject,
    SandboxExecutor,
    safe_list,
    safe_read,
    safe_write,
)
from ..state import ProjectStore


# Same output caps as Coder — keep individual tool responses manageable.
_DEFAULT_FILE_READ_BYTES = 80_000
_DEFAULT_LIST_ENTRIES = 200
_BASH_DEFAULT_TIMEOUT = 60
_BASH_MAX_TIMEOUT = 300


@dataclass
class ReviewSignal:
    """What submit_review captures. Populated by the tool, read by the Reviewer agent."""

    outcome: str = ""  # "approve" | "request_changes"
    summary: str = ""  # brief human-readable summary
    findings: list[str] = field(default_factory=list)  # specific issues when request_changes


def build_reviewer_tools(
    store: ProjectStore,
    sandbox: SandboxExecutor,
    task: dict[str, Any],
    *,
    signal_receiver: Callable[[ReviewSignal], None],
) -> list[ToolSpec]:
    """Build the Reviewer's tool set for a single task review.

    `signal_receiver` is a callback submit_review invokes to deliver the verdict.
    The review agent loop ends when the signal is received, mirroring how the
    Coder's outcome_receiver works.
    """

    # ---- read_task ------------------------------------------------------------

    async def read_task_exec(_args: dict[str, Any]) -> ToolResult:
        import json as _json

        view = {
            "id": task["id"],
            "phase": task["phase"],
            "title": task["title"],
            "description": task["description"],
            "acceptance_criteria": task["acceptance_criteria"],
            "iterations_so_far": task.get("iterations", 0),
            "review_cycles_so_far": task.get("review_cycles", 0),
        }
        # Surface prior Reviewer findings on rework loops so the Reviewer sees what
        # was flagged last time and can specifically check whether it was addressed.
        prior_findings = []
        try:
            decisions = await store.read_decisions()
            for entry in reversed(decisions):
                if (
                    entry.get("kind") == "review_request_changes"
                    and entry.get("task_id") == task["id"]
                ):
                    prior_findings.append(
                        {
                            "cycle": entry.get("cycle"),
                            "findings": entry.get("findings", []),
                            "summary": entry.get("summary", ""),
                        }
                    )
                    if len(prior_findings) >= 3:
                        break
        except Exception:
            # If decisions log can't be read for any reason, proceed without history —
            # the Reviewer can still do its job, just without memory of prior reviews.
            pass
        if prior_findings:
            view["prior_review_findings"] = list(reversed(prior_findings))

        return ToolResult(content=_json.dumps(view, indent=2))

    # ---- fs_list --------------------------------------------------------------

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

    # ---- fs_read --------------------------------------------------------------

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

    # ---- write_verification_script -------------------------------------------

    # Root for Reviewer-authored scripts. Everything the Reviewer writes goes here;
    # this keeps the Reviewer out of the actual project source and makes cleanup
    # straightforward (just nuke this dir after the task).
    scratch_relative_root = f".devteam/review-scratch/{task['id']}"

    async def write_verification_script_exec(args: dict[str, Any]) -> ToolResult:
        filename = args.get("filename")
        content = args.get("content")
        if not filename or not isinstance(filename, str):
            return ToolResult(
                content="Missing 'filename' (e.g., 'verify.py')",
                is_error=True,
            )
        # Disallow path separators so the Reviewer can't escape the scratch dir.
        # The tool is "write a script" not "write anywhere".
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return ToolResult(
                content=(
                    "filename must be a simple name like 'verify.py' — no slashes, "
                    "no leading dot, no subdirectories."
                ),
                is_error=True,
            )
        if content is None or not isinstance(content, str):
            return ToolResult(
                content="Missing 'content' — the full text of the script",
                is_error=True,
            )

        rel_path = f"{scratch_relative_root}/{filename}"
        try:
            resolved = safe_write(sandbox.project_root, rel_path, content)
        except PathOutsideProject as exc:
            return ToolResult(content=str(exc), is_error=True)
        except OSError as exc:
            return ToolResult(content=f"Write failed: {exc}", is_error=True)

        await store.append_decision(
            {
                "actor": "reviewer",
                "kind": "verification_script_written",
                "task_id": task["id"],
                "path": rel_path,
                "bytes": len(content.encode("utf-8")),
            }
        )
        return ToolResult(
            content=(
                f"Wrote {len(content)} chars to {rel_path}. "
                f"Run it with: bash argv=['python', '{rel_path}']"
            )
        )

    # ---- bash -----------------------------------------------------------------

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

        await store.append_decision(
            {
                "actor": "reviewer",
                "kind": "bash",
                "task_id": task["id"],
                "argv": argv,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
            }
        )

        status_line = (
            f"timed out after {timeout_seconds}s"
            if result.timed_out
            else f"exit={result.exit_code}"
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        # Truncate very long output so one command can't blow up context
        def _trunc(s: str, n: int = 8000) -> str:
            return s if len(s) <= n else s[:n] + f"\n[... truncated {len(s) - n} chars]"

        parts = [f"[{status_line}, {result.duration_ms}ms]"]
        if stdout:
            parts.append(f"--- stdout ---\n{_trunc(stdout)}")
        if stderr:
            parts.append(f"--- stderr ---\n{_trunc(stderr)}")
        return ToolResult(
            content="\n".join(parts),
            is_error=result.exit_code != 0 and not result.timed_out,
        )

    # ---- submit_review --------------------------------------------------------

    async def submit_review_exec(args: dict[str, Any]) -> ToolResult:
        outcome = args.get("outcome")
        summary = (args.get("summary") or "").strip()
        findings = args.get("findings") or []

        if outcome not in ("approve", "request_changes"):
            return ToolResult(
                content=(
                    "outcome must be 'approve' or 'request_changes'. "
                    "'approve' means the task meets every acceptance criterion with "
                    "observable verification. 'request_changes' means at least one "
                    "criterion is not met or tests don't actually test the behavior."
                ),
                is_error=True,
            )
        if not summary:
            return ToolResult(
                content="summary is required — a one-sentence recap of what you verified.",
                is_error=True,
            )
        if outcome == "request_changes":
            if not isinstance(findings, list) or len(findings) == 0:
                return ToolResult(
                    content=(
                        "request_changes requires findings: a non-empty list of specific, "
                        "actionable issues for the Coder to address. Each finding should "
                        "name the file/function involved and what's wrong."
                    ),
                    is_error=True,
                )
            for i, f in enumerate(findings):
                if not isinstance(f, str) or not f.strip():
                    return ToolResult(
                        content=f"findings[{i}] must be a non-empty string",
                        is_error=True,
                    )
            clean_findings = [f.strip() for f in findings]
        else:
            clean_findings = []

        signal_receiver(
            ReviewSignal(
                outcome=outcome,
                summary=summary,
                findings=clean_findings,
            )
        )
        return ToolResult(
            content=(
                f"Review submitted: {outcome}. "
                + (
                    "Task will proceed to done."
                    if outcome == "approve"
                    else f"Task will go back to Coder with {len(clean_findings)} finding(s)."
                )
            )
        )

    # ---- tool specs -----------------------------------------------------------

    return [
        ToolSpec(
            name="read_task",
            description=(
                "Read the current task — id, title, description, acceptance criteria, "
                "and any findings from prior review cycles. ALWAYS call this first. "
                "The acceptance criteria are the only things that determine approve vs "
                "request_changes; everything else is context."
            ),
            input_schema={"type": "object", "properties": {}},
            executor=read_task_exec,
        ),
        ToolSpec(
            name="fs_list",
            description=(
                "List files in a directory inside the project (relative to project root). "
                "Use to see what the Coder produced before reading specific files."
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
                "(default 80KB). Use to inspect the code the Coder wrote and the "
                "tests that should verify it."
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
            name="write_verification_script",
            description=(
                "Write a verification script under .devteam/review-scratch/<task_id>/. "
                "Use this whenever you need multi-line logic to check the Coder's work "
                "— CRITICAL on Windows where `python -c` mangles newlines and multi-line "
                "scripts fail. Write the script with this tool, then run it with "
                "bash argv=['python', '.devteam/review-scratch/<task_id>/<filename>']. "
                "You cannot modify the Coder's project files — this scratch directory "
                "is the only place you can write. Scripts here are auto-cleaned when "
                "the task is marked done.\n\n"
                "Example: write a script named 'verify_endpoint.py' that starts the "
                "server, curls an endpoint, and asserts the response, instead of trying "
                "to cram all that into a single `python -c` call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": (
                            "Simple filename like 'verify.py' or 'check_output.sh'. "
                            "No slashes, no leading dot — just the name."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Full text of the script",
                    },
                },
                "required": ["filename", "content"],
            },
            executor=write_verification_script_exec,
        ),
        ToolSpec(
            name="bash",
            description=(
                "Run a command in the sandbox. This is NOT a shell — pass argv as a list "
                "of strings. Example: {'argv': ['pytest', '-q']}. Use this to actually "
                "run the tests the Coder wrote (do not trust the Coder's claim they pass), "
                "run linters, start a server and curl an endpoint, etc. Only whitelisted "
                "commands are allowed. Default timeout 60s.\n\n"
                "IMPORTANT — `python -c` limitation on Windows: passing multi-line scripts "
                "to `python -c` breaks because Windows mangles newlines in argv. If you "
                "need more than one statement, use write_verification_script to write a "
                ".py file, then run it with argv=['python', '<path-to-script>']."
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
            name="submit_review",
            description=(
                "End the review with your verdict. outcome='approve' ONLY when all three "
                "hard rules are satisfied: (1) zero confirmed defects, (2) tests don't mock "
                "the thing under test, (3) any runnable artifact has been actually run and "
                "behaves correctly. outcome='request_changes' for any shortfall — hard "
                "threshold, no 'overall good enough despite X', no 'minor issue, approve "
                "with notes'. When requesting changes, findings must be specific and "
                "actionable: 'Test in file X at line Y mocks the DB so it doesn\\'t verify "
                "criterion Z' beats 'tests could be better'. Call this exactly once at the end."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["approve", "request_changes"],
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "One-sentence recap. For approve: what you verified. For "
                            "request_changes: the headline problem."
                        ),
                    },
                    "findings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Required when outcome='request_changes'. Each finding "
                            "names the file/function and what's wrong in a way the "
                            "Coder can act on without follow-up questions."
                        ),
                    },
                },
                "required": ["outcome", "summary"],
            },
            executor=submit_review_exec,
        ),
    ]
