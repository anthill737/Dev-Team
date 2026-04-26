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
    playwright_enabled: bool = False,
) -> list[ToolSpec]:
    """Build the Reviewer's tool set for a single task review.

    `signal_receiver` is a callback submit_review invokes to deliver the verdict.
    The review agent loop ends when the signal is received, mirroring how the
    Coder's outcome_receiver works.

    `playwright_enabled` controls whether the playwright_check tool is exposed
    to the Reviewer. When False (default), only the standard read/bash tools
    are available; the Reviewer's prompt also tells it Playwright is OFF so
    it doesn't try to call a tool that isn't there. When True, Playwright is
    available and the prompt instructs the Reviewer to use it for any task
    with a browser-rendered artifact.
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

    # ---- playwright_check ----------------------------------------------------
    #
    # Browser-based runtime verification. Loads a URL (typically a local
    # file:// or http://localhost:* served by the Coder's build) in a headless
    # Chromium, optionally runs a small click/input script, and returns:
    #   - the page title
    #   - first 5 console error messages
    #   - first 200 chars of body innerText (proof the DOM populated)
    #   - any uncaught JS errors during the run
    #
    # Designed to catch the "green tests, black screen" failure mode: the
    # build completes, unit tests pass, but the actual rendered page is
    # broken. A real Reviewer running this gets ground-truth verification.
    #
    # Runs via subprocess (not in-process) so the parent process doesn't get
    # stuck on Playwright's threading model. Spawns a dedicated Python script
    # with a tight timeout. If Playwright isn't installed, returns an error
    # the Reviewer reads and surfaces to the user.

    async def playwright_check_exec(args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        wait_ms = args.get("wait_ms", 1500)
        if not isinstance(url, str) or not url:
            return ToolResult(
                content="Missing 'url'. Provide a file:// or http:// URL.",
                is_error=True,
            )
        if not isinstance(wait_ms, int) or wait_ms < 0 or wait_ms > 30000:
            return ToolResult(
                content="'wait_ms' must be an integer 0-30000.",
                is_error=True,
            )

        # Launch a one-shot Python subprocess running the Playwright check.
        # Subprocess isolates Playwright's asyncio loop from ours and lets
        # us hard-kill on timeout.
        import asyncio as _asyncio
        import json as _json
        import shutil as _shutil
        import sys as _sys

        script = f'''
import asyncio, json, sys
async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(json.dumps({{
            "error": "playwright_not_installed",
            "hint": "Install on the host: pip install playwright && playwright install chromium"
        }}))
        return
    console_errors = []
    page_errors = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            print(json.dumps({{"error": "browser_launch_failed", "detail": str(e)}}))
            return
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page.on("console", lambda msg: console_errors.append(f"{{msg.type}}: {{msg.text}}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            await page.goto({_json.dumps(url)}, wait_until="load", timeout=15000)
        except Exception as e:
            await browser.close()
            print(json.dumps({{"error": "navigation_failed", "detail": str(e)}}))
            return
        await page.wait_for_timeout({wait_ms})
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body_text = (await page.inner_text("body"))[:200]
        except Exception:
            body_text = ""
        await browser.close()
        print(json.dumps({{
            "title": title,
            "body_excerpt": body_text,
            "console_errors": console_errors[:5],
            "page_errors": page_errors[:5],
        }}))
asyncio.run(main())
'''

        try:
            proc = await _asyncio.create_subprocess_exec(
                _sys.executable, "-c", script,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                cwd=str(sandbox.project_root),
            )
            try:
                stdout, stderr = await _asyncio.wait_for(
                    proc.communicate(), timeout=45.0
                )
            except _asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    content=(
                        "playwright_check timed out after 45s. The page may be "
                        "stuck in an infinite loop, or the URL is unreachable. "
                        "Try a smaller wait_ms or verify the URL is correct."
                    ),
                    is_error=True,
                )

            output = stdout.decode("utf-8", errors="replace").strip()
            if not output:
                err = stderr.decode("utf-8", errors="replace")[:500]
                return ToolResult(
                    content=f"playwright_check produced no output. Stderr: {err}",
                    is_error=True,
                )
            try:
                result = _json.loads(output.splitlines()[-1])
            except _json.JSONDecodeError:
                return ToolResult(
                    content=f"playwright_check returned non-JSON output:\n{output[:500]}",
                    is_error=True,
                )

            # Translate the structured result into a single readable text
            # block — easier for the model to reason against than raw JSON.
            if "error" in result:
                if result["error"] == "playwright_not_installed":
                    return ToolResult(
                        content=(
                            "Playwright is not installed in this environment. "
                            "Install it yourself using bash, then retry "
                            "playwright_check — do NOT submit a review yet, "
                            "and do NOT request_changes for this. The install "
                            "is two commands:\n\n"
                            "  1. bash argv=['pip', 'install', 'playwright']\n"
                            "  2. bash argv=['playwright', 'install', 'chromium']\n\n"
                            "Step 2 downloads ~150MB of browser binary so set "
                            "timeout_seconds=600 (10 min). Once both succeed, "
                            "call playwright_check again on the same URL. "
                            "Treat this as a one-time bootstrap step the team "
                            "owns — not a defect against the Coder's task."
                        ),
                        is_error=True,
                    )
                return ToolResult(
                    content=(
                        f"playwright_check error: {result['error']}\n"
                        f"Detail: {result.get('detail', '')}"
                    ),
                    is_error=True,
                )

            # Success path. Format observations.
            lines = [
                f"Loaded {url}",
                f"  title: {result.get('title', '') or '(empty)'}",
                f"  body excerpt: {result.get('body_excerpt', '') or '(empty)'}",
            ]
            console_errors = result.get("console_errors", [])
            page_errors = result.get("page_errors", [])
            if page_errors:
                lines.append(f"  page errors ({len(page_errors)}):")
                for e in page_errors:
                    lines.append(f"    - {e[:200]}")
            else:
                lines.append("  page errors: none")
            if console_errors:
                lines.append(f"  console errors ({len(console_errors)}):")
                for e in console_errors:
                    lines.append(f"    - {e[:200]}")
            else:
                lines.append("  console errors: none")

            # Treat ANY page error or console error as suspicious — surface
            # it as a non-error tool result, but the Reviewer's rules say
            # one defect = REJECT, so they should reject on this signal.
            return ToolResult(content="\n".join(lines))

        except FileNotFoundError:
            return ToolResult(
                content=(
                    "Could not launch Python subprocess for playwright_check. "
                    "This is a backend environment issue."
                ),
                is_error=True,
            )

    # ---- tool specs -----------------------------------------------------------

    specs: list[ToolSpec] = [
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
                "On Windows, run .bat files via {'argv': ['cmd', '/c', 'run.bat']} and "
                ".ps1 files via {'argv': ['powershell', '-NoProfile', '-File', 'run.ps1']}. "
                "These are the supported shapes — inline -Command / -c \"...\" execution "
                "is blocked. To verify a Windows project's run script, use cmd /c.\n\n"
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

    # Only register playwright_check when explicitly enabled. Keeping it out
    # of the tool list when off ensures the model can't try to call a tool
    # that won't work — and the prompt's "Step 0" tells the model whether
    # the tool is available, mirroring the runtime state.
    if playwright_enabled:
        specs.append(
            ToolSpec(
                name="playwright_check",
                description=(
                    "Load a URL in a headless Chromium browser and report what "
                    "actually rendered. Use this for ANY task with a browser-rendered "
                    "artifact (Three.js scene, React UI, canvas game, plain HTML). "
                    "This is the verification path Rule 3 requires when Playwright is "
                    "ON for this project. Returns: page title, body text excerpt, "
                    "console error messages, and uncaught JS errors. ANY console error "
                    "or page error means the page is broken — apply Rule 1 and reject. "
                    "An empty body_excerpt or empty title on a page that should display "
                    "content is also a defect. URL can be file:// (e.g., the project's "
                    "dist/index.html) or http://localhost:PORT (after the Coder starts "
                    "a dev server). wait_ms is how long to let JS execute after load — "
                    "1500ms is fine for static pages, bump to 3000-5000 for games or "
                    "heavy SPAs that render asynchronously."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "URL to load. file:// for local files, http:// for "
                                "running servers. Must be a string."
                            ),
                        },
                        "wait_ms": {
                            "type": "integer",
                            "description": (
                                "Milliseconds to wait after page load for JS to "
                                "execute. Default 1500. Max 30000."
                            ),
                            "default": 1500,
                        },
                    },
                    "required": ["url"],
                },
                executor=playwright_check_exec,
            )
        )

    return specs
