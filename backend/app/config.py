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

    # Default model assignments per role. Overridable per project.
    model_architect: str = "claude-sonnet-4-6"
    model_dispatcher: str = "claude-sonnet-4-6"
    model_coder: str = "claude-sonnet-4-6"
    model_reviewer: str = "claude-opus-4-7"

    # Default budgets. Overridable per project during setup.
    default_task_token_budget: int = 50_000
    default_project_token_budget: int = 2_000_000
    default_max_task_iterations: int = 5

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
