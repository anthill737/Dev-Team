"""Tests for platform-aware prompts.

Verifies that:
  - coder_prompt(user_platform=...) injects the right shell-syntax hints
  - reviewer_prompt does the same
  - Windows doesn't leak bash-isms, macOS/Linux don't leak PowerShell-isms
  - Unknown/missing platform falls back to linux without error
"""

from __future__ import annotations

from app.prompts.library import coder_prompt, reviewer_prompt


def test_coder_windows_has_powershell_hints() -> None:
    p = coder_prompt(user_platform="windows")
    assert "PowerShell" in p
    # Key PowerShell-specific guidance
    assert "$env:VAR" in p
    assert "Activate.ps1" in p
    # Windows hint DOES mention `source .venv/bin/activate` as an anti-example
    # ("never source .venv/bin/activate"). Check that the framing is negative:
    # the Windows version doesn't have the macOS/Linux phrasing "activate venvs with".
    assert "Activate venvs with `source" not in p


def test_coder_macos_has_bash_hints() -> None:
    p = coder_prompt(user_platform="macos")
    assert "macOS" in p
    # Check for the positive instructional phrasing
    assert "Activate venvs with `source .venv/bin/activate`" in p
    # Should NOT have PowerShell as the target
    assert "Windows PowerShell**" not in p
    assert "$env:VAR = \"value\"" not in p  # positive PowerShell framing absent
    assert "Activate.ps1" not in p or "NOT" in p  # if mentioned, only as anti-example


def test_coder_linux_has_bash_hints() -> None:
    p = coder_prompt(user_platform="linux")
    assert "**Linux**" in p
    assert "Activate venvs with `source .venv/bin/activate`" in p
    assert "Windows PowerShell**" not in p


def test_coder_unknown_platform_falls_back_to_linux() -> None:
    """Prompt build must never fail on an unexpected platform string — fall
    back to linux-style hints so the Coder still has usable guidance."""
    p = coder_prompt(user_platform="beos")
    assert "source .venv/bin/activate" in p  # linux fallback applied


def test_coder_no_placeholder_leaks() -> None:
    """The placeholder token must be fully replaced — a literal '{PLATFORM_HINTS}'
    in the rendered prompt would be a template bug."""
    for plat in ("windows", "macos", "linux"):
        p = coder_prompt(user_platform=plat)
        assert "{PLATFORM_HINTS}" not in p


def test_reviewer_windows_has_short_powershell_hints() -> None:
    p = reviewer_prompt(user_platform="windows")
    assert "PowerShell" in p
    assert "$env:VAR" in p


def test_reviewer_linux_has_short_bash_hints() -> None:
    p = reviewer_prompt(user_platform="linux")
    assert "**Linux**" in p
    # Positive guidance present
    assert "standard bash syntax" in p
    # No positive Windows framing — Windows is only mentioned as an anti-example
    # ("Do NOT use Windows PowerShell idioms"), not as the user's platform.
    assert "user is on **Windows" not in p


def test_reviewer_no_placeholder_leaks() -> None:
    for plat in ("windows", "macos", "linux"):
        p = reviewer_prompt(user_platform=plat)
        assert "{PLATFORM_HINTS_SHORT}" not in p
