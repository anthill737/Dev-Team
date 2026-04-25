"""Application configuration, loaded from environment and per-project settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global backend configuration, loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVTEAM_", extra="ignore")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Projects — where project metadata is tracked (outside the project dirs themselves).
    # Lives next to the app so the whole installation is self-contained; users who
    # want it elsewhere can override via DEVTEAM_PROJECTS_REGISTRY_PATH.
    projects_registry_path: str = str(
        Path(__file__).resolve().parent.parent.parent / ".devteam-run" / "projects.json"
    )

    # Default model assignments per role. Overridable per project via the
    # project's settings UI; persisted in the project's meta.json.
    #
    # Architect/Reviewer default to Opus 4.7 — both roles benefit most from
    # the highest-capability model. The Architect's plan shapes everything
    # downstream; subtle errors during the interview translate to weeks of
    # wrong work. The Reviewer is the safety net for the Coder; downgrading
    # it loses the catch rate that makes mandatory review valuable.
    #
    # Dispatcher/Coder default to Sonnet 4.6 — heavy enough on tool use and
    # decomposition that Haiku is risky, but where Opus's marginal capability
    # over Sonnet doesn't justify the quota burn. Per-task code-writing rounds
    # are the bulk of the workload, so this is where Sonnet pays off most.
    model_architect: str = "claude-opus-4-7"
    model_dispatcher: str = "claude-sonnet-4-6"
    model_coder: str = "claude-sonnet-4-6"
    model_reviewer: str = "claude-opus-4-7"

    # Agent runner selection. Two options:
    #
    #   "claude_code" (default): drive agents via the user's Claude Code CLI
    #     subscription. No per-token billing — usage counts against Pro/Max
    #     quota (5-hour rolling window). Requires `claude` CLI installed and
    #     authenticated (`claude setup-token` or interactive `claude` login).
    #
    #   "api": drive agents via direct Anthropic API calls using an API key.
    #     Pay-per-token billing. Set DEVTEAM_API_KEY or per-request header.
    #
    # When set to "claude_code", the API-key path is hard-disabled at runtime
    # to prevent accidental billing — API requests short-circuit with an error
    # even if DEVTEAM_API_KEY is populated in the environment.
    runner: str = Field(default="claude_code", pattern="^(claude_code|api)$")

    # Default budgets. Overridable per project during setup.
    # Budget defaults. These are the numbers applied when a user creates a
    # project without overriding them. Chosen based on real-world runs:
    #   - 150k/task is enough for most tasks that aren't trivially small.
    #     Anything below ~75k routinely runs out mid-task on non-toy work.
    #   - 5M project budget covers a normal MVP run (7-15 tasks, some review
    #     cycles). Well under pricing concerns for Sonnet/Haiku work.
    #   - 8 iterations lets the Coder recover from a couple of blind alleys
    #     before escalating to the user. 5 was too tight in practice.
    default_task_token_budget: int = 150_000
    default_project_token_budget: int = 5_000_000
    default_max_task_iterations: int = 8

    # Sandbox
    sandbox_image: str = "devteam-sandbox:latest"
    sandbox_network_mode: str = "bridge"  # restrict further for prod
    sandbox_memory_limit: str = "2g"
    sandbox_cpu_limit: float = 2.0

    # Logging
    log_level: str = "INFO"

    # Rate limits on the app itself (rudimentary; real budgeting is per-project)
    max_concurrent_agents_per_project: int = Field(default=2, ge=1, le=8)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def detect_host_platform() -> str:
    """Return the current backend host's platform as 'windows', 'macos', or 'linux'.

    Used at project-creation time to pick sensible defaults so Coder/Reviewer
    hand commands to the user in the right shell syntax. Stored per project
    so sharing a codebase across machines doesn't matter — the platform is
    wherever the project was created.
    """
    import sys

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ---------------------------------------------------------------------------
# Model catalog — the single source of truth for valid model strings the
# project-settings UI can offer per agent.
#
# Adding a new model: append the string here. Removing one: take it out of
# this list AND make sure no project's meta.json references it (the validator
# will refuse the update if it's not in this list).
#
# The "label" is what the UI shows users. The "string" is what the runner
# passes to Claude Code via ClaudeAgentOptions.model.
#
# Cost notes are heuristic and meant to inform users; actual quota burn
# depends on prompt length, tool calls, etc.
MODEL_CHOICES: list[dict[str, str]] = [
    {
        "string": "claude-opus-4-7",
        "label": "Opus 4.7",
        "cost_hint": "highest quality, most quota usage",
    },
    {
        "string": "claude-sonnet-4-6",
        "label": "Sonnet 4.6",
        "cost_hint": "balanced — good default for most roles",
    },
    {
        "string": "claude-haiku-4-5-20251001",
        "label": "Haiku 4.5",
        "cost_hint": "cheapest, fastest, weakest — risky for Architect/Reviewer",
    },
]


# Roles that can have model overrides. Used by the project-settings validator
# to reject unknown role keys, and by the UI to render one dropdown per role.
AGENT_ROLES: tuple[str, ...] = ("architect", "dispatcher", "coder", "reviewer")


def valid_model_strings() -> set[str]:
    """Set of valid model strings, for quick membership checks in validators."""
    return {m["string"] for m in MODEL_CHOICES}
