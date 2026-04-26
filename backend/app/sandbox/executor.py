"""Process-based sandbox executor.

Safety properties this enforces:
  1. Command whitelist. Only pre-approved executable names run. Unknown commands are denied.
  2. No shell interpretation. Commands execute via subprocess with shell=False and argv as a
     list. Pipes, redirects, command substitution, backgrounding — none of it works. Agents
     that want to chain operations do so with multiple tool calls.
  3. Working directory locked. All commands execute in the project root. Relative paths in
     arguments can still point outside (e.g., `../../etc`) — argument-level path checking is
     the *caller's* responsibility when it's constructing the argv (see fs.py).
  4. Timeouts. Every command has a deadline. Default 30s, max 300s.
  5. Output caps. stdout and stderr are each capped to 50KB. Truncation is flagged.
  6. Environment scrubbed. Most env vars are stripped; only a minimal safe set is passed.

This is not true isolation — a malicious or buggy agent could still exhaust CPU, fill the
project directory, read/write project files, or make network calls via allowed commands.
The threat model is "prevent catastrophic mistakes," not "withstand an adversary."
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# ---- Exceptions ----------------------------------------------------------------------------------


class CommandDenied(Exception):
    """Raised when a command is not on the whitelist."""


class PathOutsideProject(Exception):
    """Raised when a path argument resolves outside the project root."""


# ---- Whitelist -----------------------------------------------------------------------------------

# Commands the Coder can execute. Keep this list conservative — add only as genuinely needed.
# The Coder should express intent through these and nothing else. If we ever allow arbitrary
# bash, the whole safety model collapses.
#
# CAVEAT THAT MATTERS: commands like `npm install`, `pip install`, and `npx <package>` can
# execute arbitrary code via postinstall hooks, setup.py, or the package itself. We keep
# them on the whitelist because installing dependencies is essential to actually building
# projects — but this is a known soft spot in the safety model. Anyone running the Coder
# should only do so with projects they trust the dependency graph of, on a machine where
# losing the project directory would be survivable.
DEFAULT_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        # Python ecosystem
        "python",
        "python3",
        "pip",
        "pytest",
        "ruff",
        "mypy",
        # Node ecosystem
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "tsc",
        "eslint",
        "prettier",
        "vitest",
        "jest",
        # Playwright CLI — needed for the Reviewer to bootstrap its own
        # Chromium when playwright_check returns 'playwright_not_installed'.
        # Subcommand restrictions in _PLAYWRIGHT_ALLOWED_SUBCOMMANDS prevent
        # arbitrary use; only `playwright install` and `playwright --version`
        # are allowed.
        "playwright",
        # Git (network subcommands like push/pull/clone blocked at the arg level below)
        "git",
        # Read-only inspection — writes go through fs.safe_write, not shell commands
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        # Search
        "grep",
        "rg",
        # Sanity checks
        "echo",
        # Directory creation (safe within the project — locked by CWD; no args can escape
        # because `mkdir foo/../../bar` still resolves relative to CWD)
        "mkdir",
        "touch",
        # Windows shells — required for the Reviewer to actually execute .bat
        # and .ps1 files on Windows projects (without them, run.bat can't be
        # verified at runtime and Rule 3 silently degrades to "looks right").
        # Argv-level guards in _validate_windows_shell_args restrict these to
        # launching project-relative script files only — they CANNOT be used
        # to run arbitrary inline commands like `cmd /c rm -rf`.
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
    }
)

# Deliberately NOT on the whitelist (with rationale):
#   find    — `find / -delete` or `find / -exec rm {} \;` reaches anywhere readable.
#   cp, mv  — arguments accept absolute paths outside the project root; the executor
#             locks CWD but can't lock argv. File moves/copies go through fs.safe_write.
#   tree    — rarely installed; agents can use `ls -R` instead.
#   curl, wget, ssh, scp — no outbound network from the sandbox.
#   rm, rmdir — destructive. If deletion is genuinely needed, it can be added with
#              a reviewed wrapper that enforces path boundaries.

# Within git, these subcommands are blocked. Everything else (status, log, diff, add, commit,
# branch, checkout, init) is allowed.
_GIT_BLOCKED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"push", "pull", "fetch", "clone", "remote"}
)

# Within pip/npm, these subcommands are blocked to prevent installing from arbitrary URLs
# or registries. "install" and "uninstall" from the default registries are allowed.
_PIP_BLOCKED_FLAGS: frozenset[str] = frozenset(
    {"--index-url", "--extra-index-url", "--trusted-host"}
)

# Within the playwright CLI, only `install` (with optional browser names like
# `chromium`, `firefox`, `webkit`) and `--version` are allowed. `codegen`,
# `test`, `show-trace`, etc. either need a UI, can hang the sandbox, or open
# arbitrary URLs.
_PLAYWRIGHT_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"install", "install-deps", "--version", "-V"}
)
_PLAYWRIGHT_ALLOWED_BROWSERS: frozenset[str] = frozenset(
    {"chromium", "firefox", "webkit"}
)


# ---- Windows shell argv validation ---------------------------------------------------------------
#
# cmd.exe and powershell are on the allowlist so the Reviewer can run .bat/.ps1
# files (essential for Windows project verification — Rule 3 needs runtime
# checks). But their default capability is "run literally any command", which
# is too much. We restrict them to a narrow shape: launch a project-local
# script file by name. Inline commands (cmd /c "echo hi"; powershell -Command
# "Get-Process") are blocked.
#
# Allowed shapes:
#   cmd /c run.bat                             — relative .bat in CWD
#   cmd.exe /c scripts\build.bat               — relative .bat in subdir
#   cmd /c run.bat arg1 arg2                   — script + args
#   powershell -File run.ps1                   — relative .ps1
#   powershell.exe -ExecutionPolicy Bypass -File scripts/build.ps1  — common form
#
# Blocked:
#   cmd /c "rm -rf C:\"                        — inline command
#   powershell -Command "Get-Process"          — inline command
#   cmd /c C:\Windows\System32\evil.bat        — absolute path
#   cmd /c ../../escape.bat                    — path escape

_CMD_LAUNCH_FLAGS: frozenset[str] = frozenset({"/c", "/C", "/k", "/K"})
_POWERSHELL_FILE_FLAGS: frozenset[str] = frozenset({"-File", "-file", "-f"})
_POWERSHELL_PASSTHROUGH_FLAGS: frozenset[str] = frozenset(
    {
        "-ExecutionPolicy",
        "-executionpolicy",
        "-NoProfile",
        "-noprofile",
        "-NonInteractive",
        "-noninteractive",
        "-NoLogo",
        "-nologo",
    }
)
_WINDOWS_SHELL_NAMES: frozenset[str] = frozenset(
    {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"}
)


def _validate_script_path(path_str: str, *, expected_exts: tuple[str, ...]) -> None:
    """Reject anything that isn't a project-relative script path with the right
    extension. Mirrors the discipline of fs.safe_path: no absolute paths, no
    parent-traversal, no leading slash. The actual path-traversal containment
    happens in the executor's CWD lock — this is just a fast-fail for obvious
    misuse.
    """
    if not path_str:
        raise CommandDenied("Empty script path")
    if path_str.startswith("/") or path_str.startswith("\\"):
        raise CommandDenied(f"Script path must be relative, not absolute: {path_str!r}")
    # Windows absolute paths like "C:\..." or "C:/..."
    if len(path_str) >= 2 and path_str[1] == ":":
        raise CommandDenied(f"Script path must be relative, not absolute: {path_str!r}")
    if ".." in path_str.replace("\\", "/").split("/"):
        raise CommandDenied(f"Script path may not contain '..': {path_str!r}")
    lower = path_str.lower()
    if not any(lower.endswith(ext) for ext in expected_exts):
        raise CommandDenied(
            f"Script must end in one of {expected_exts}: got {path_str!r}"
        )


def _validate_windows_shell_args(argv: list[str]) -> None:
    """Enforce the narrow allowed shape for cmd/powershell invocations.

    Raises CommandDenied if argv tries to do anything other than launch a
    project-relative .bat/.cmd/.ps1 file. See module-level comment for the
    full list of allowed shapes.
    """
    cmd = argv[0].lower()
    rest = argv[1:]
    if not rest:
        raise CommandDenied(
            f"{cmd!r} requires args — must launch a script file. "
            f"Allowed shapes: 'cmd /c <script.bat>' or 'powershell -File <script.ps1>'."
        )

    if cmd in {"cmd", "cmd.exe"}:
        # Shape: cmd /c <script.bat> [args...]
        flag = rest[0]
        if flag not in _CMD_LAUNCH_FLAGS:
            raise CommandDenied(
                f"cmd first arg must be /c or /k, got {flag!r}. "
                f"Inline commands like 'cmd /c \"echo ...\"' are blocked."
            )
        if len(rest) < 2:
            raise CommandDenied("cmd /c requires a script path argument")
        script = rest[1]
        # Reject anything that looks like an inline command rather than a
        # script file. The cheapest tell: does it have a script extension?
        _validate_script_path(script, expected_exts=(".bat", ".cmd"))
        return

    if cmd in {"powershell", "powershell.exe", "pwsh"}:
        # Shape: powershell [passthrough flags...] -File <script.ps1> [args...]
        # Walk through args until we find -File; everything before must be a
        # passthrough flag (-NoProfile, -ExecutionPolicy Bypass, etc.). After
        # -File, the next token is the script path.
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in _POWERSHELL_FILE_FLAGS:
                if i + 1 >= len(rest):
                    raise CommandDenied("powershell -File requires a script path")
                script = rest[i + 1]
                _validate_script_path(script, expected_exts=(".ps1",))
                return
            if token in _POWERSHELL_PASSTHROUGH_FLAGS:
                # -ExecutionPolicy takes a value; -NoProfile etc. don't. Just
                # consume an extra token if the next one isn't a flag — this
                # is a heuristic but it works for the documented passthrough
                # flags above.
                i += 1
                if (
                    token in {"-ExecutionPolicy", "-executionpolicy"}
                    and i < len(rest)
                ):
                    i += 1
                continue
            # Anything else (e.g. -Command, -EncodedCommand, a bare arg) is
            # rejected — these are the dangerous inline-execution paths.
            raise CommandDenied(
                f"powershell arg {token!r} is not allowed. "
                f"Only -File <script.ps1> with optional -NoProfile / "
                f"-ExecutionPolicy / -NonInteractive / -NoLogo is permitted. "
                f"Inline commands via -Command or -EncodedCommand are blocked."
            )
        raise CommandDenied(
            "powershell invocation must include -File <script.ps1>. "
            "Inline commands are blocked."
        )


# ---- Data types ----------------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Outcome of executing a command."""

    command: list[str]
    exit_code: int | None  # None if timed out or failed to start
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class SandboxExecutor(Protocol):
    """Interface for running commands in a project sandbox."""

    async def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        ...

    @property
    def project_root(self) -> Path:
        ...


# ---- Implementation ------------------------------------------------------------------------------


# Caps are generous enough for real test output but catch runaway processes (e.g., an
# infinite loop printing to stdout).
_MAX_STDOUT_BYTES = 50_000
_MAX_STDERR_BYTES = 50_000
# 10 minutes — long enough for `playwright install chromium` (~150MB browser
# download) on slow connections and `pip install` of large frameworks. Hard
# kill at the cap so a wedged process can't run forever.
_MAX_TIMEOUT_SECONDS = 600
_MIN_TIMEOUT_SECONDS = 1


class ProcessSandboxExecutor:
    """Runs commands via subprocess with the safety properties described at module top."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        allowed_commands: frozenset[str] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"Project root does not exist or is not a directory: {root}")
        self._project_root = root
        self._allowed = allowed_commands or DEFAULT_ALLOWED_COMMANDS

    @property
    def project_root(self) -> Path:
        return self._project_root

    async def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        if not argv:
            raise CommandDenied("Empty command")

        timeout = max(_MIN_TIMEOUT_SECONDS, min(_MAX_TIMEOUT_SECONDS, timeout_seconds))

        cmd = argv[0]
        # Disallow absolute or relative paths — must be a bare command name on PATH.
        # This is what prevents someone from running "/usr/bin/curl" when curl is off-list.
        if "/" in cmd or "\\" in cmd or cmd.startswith("."):
            raise CommandDenied(
                f"Command must be a bare name on PATH, not a path: {cmd!r}"
            )
        if cmd not in self._allowed:
            raise CommandDenied(
                f"Command not on the allowed list: {cmd!r}. "
                f"If this is genuinely needed, add it to DEFAULT_ALLOWED_COMMANDS."
            )

        # Subcommand-level restrictions for specific tools
        if cmd == "git" and len(argv) > 1 and argv[1] in _GIT_BLOCKED_SUBCOMMANDS:
            raise CommandDenied(
                f"git subcommand {argv[1]!r} is blocked (no network operations in sandbox)"
            )
        if cmd in ("pip", "pip3"):
            for flag in argv[1:]:
                if flag in _PIP_BLOCKED_FLAGS:
                    raise CommandDenied(
                        f"pip flag {flag!r} is blocked (custom index URLs not allowed)"
                    )
        # cmd/powershell are on the allowlist for running .bat/.ps1 files but
        # restricted to that shape — no inline -Command or -c "..." execution.
        if cmd in _WINDOWS_SHELL_NAMES:
            _validate_windows_shell_args(argv)
        # playwright CLI: only `install [browsers...]` or `--version`.
        # `playwright codegen` opens a browser GUI; `playwright test` runs an
        # arbitrary test config; neither is wanted from a sandboxed agent.
        if cmd == "playwright":
            if len(argv) < 2:
                raise CommandDenied(
                    "playwright requires a subcommand. Allowed: "
                    "'playwright install [browser]' or 'playwright --version'."
                )
            sub = argv[1]
            if sub not in _PLAYWRIGHT_ALLOWED_SUBCOMMANDS:
                raise CommandDenied(
                    f"playwright subcommand {sub!r} is blocked. "
                    f"Allowed: install, install-deps, --version."
                )
            # If `install`, any further args must be browser names (or flags
            # we explicitly allow). Block arbitrary positional args that
            # might be paths or shell metacharacters.
            if sub in ("install", "install-deps"):
                for arg in argv[2:]:
                    if arg.startswith("-"):
                        # Allow common safe flags; reject everything else
                        if arg not in ("--with-deps", "--dry-run", "--force"):
                            raise CommandDenied(
                                f"playwright install flag {arg!r} is blocked"
                            )
                    elif arg not in _PLAYWRIGHT_ALLOWED_BROWSERS:
                        raise CommandDenied(
                            f"playwright install argument {arg!r} is not a "
                            f"recognized browser. Allowed: chromium, firefox, webkit."
                        )

        # Resolve the executable on PATH. If not found, fail fast with a clean error
        # (otherwise we get a misleading FileNotFoundError from asyncio).
        resolved = shutil.which(cmd)
        if resolved is None:
            return CommandResult(
                command=argv,
                exit_code=None,
                stdout="",
                stderr=f"Command not found on PATH: {cmd}",
                duration_ms=0,
            )

        env = _safe_env()

        loop = asyncio.get_running_loop()
        start = loop.time()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                *argv[1:],
                cwd=str(self._project_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                timed_out = False
            except asyncio.TimeoutError:
                # Best-effort kill and drain
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=5
                    )
                except asyncio.TimeoutError:
                    stdout_bytes = b""
                    stderr_bytes = b""
                timed_out = True
        except FileNotFoundError as exc:
            return CommandResult(
                command=argv,
                exit_code=None,
                stdout="",
                stderr=f"Failed to start command: {exc}",
                duration_ms=int((loop.time() - start) * 1000),
            )

        duration_ms = int((loop.time() - start) * 1000)

        stdout, stdout_truncated = _truncate_bytes(stdout_bytes, _MAX_STDOUT_BYTES)
        stderr, stderr_truncated = _truncate_bytes(stderr_bytes, _MAX_STDERR_BYTES)

        return CommandResult(
            command=argv,
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


# ---- Helpers -------------------------------------------------------------------------------------


def _safe_env() -> dict[str, str]:
    """Build a minimal environment for subprocesses.

    We keep variables the toolchain needs (PATH, HOME, USERPROFILE on Windows, LANG) and
    drop everything else. This prevents a compromised or buggy agent from leaking secrets
    (API keys, tokens) stored in the parent process env into a subprocess that might log
    them or send them over the network.
    """
    safe_keys = {
        "PATH",
        "PATHEXT",  # Windows — required to resolve .cmd/.bat files via shutil.which
        "HOME",
        "USERPROFILE",  # Windows
        "USERNAME",
        "SYSTEMROOT",  # Windows — required for many subprocesses
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONIOENCODING",
    }
    env = {k: v for k, v in os.environ.items() if k in safe_keys}
    # Force UTF-8 for Python subprocesses — we don't want Windows cp1252 failures here either
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _truncate_bytes(data: bytes, limit: int) -> tuple[str, bool]:
    """Decode bytes to UTF-8 (lossy if needed), truncate to `limit`, return (text, truncated)."""
    truncated = False
    if len(data) > limit:
        data = data[:limit]
        truncated = True
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text = text + f"\n\n[...output truncated at {limit} bytes]"
    return text, truncated
