"""Interface for whatever agent executes an individual task.

The execution loop is the orchestrator for "work through the task list." The piece it
*delegates to* — the thing that actually writes code, runs tests, etc. — is a `TaskRunner`.

v2 will ship a Coder implementation. This session keeps the loop and the runner decoupled
so we can unit-test the loop with a fake runner and swap the real Coder in without touching
orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol

from ..agents.base import StreamEvent
from ..sandbox import SandboxExecutor
from ..state import ProjectStore


class TaskOutcomeKind(str, Enum):
    APPROVED = "approved"  # Task done, passed review
    NEEDS_REWORK = "needs_rework"  # Review requested changes; task goes back to pending
    NEEDS_USER_REVIEW = "needs_user_review"  # Coder can't self-verify; needs human check
    BLOCKED = "blocked"  # Task can't proceed; needs user or architect intervention
    FAILED = "failed"  # Unrecoverable error (crash, network, budget hit mid-run)


@dataclass
class TaskOutcome:
    """Structured result from running one task."""

    kind: TaskOutcomeKind
    tokens_input: int = 0
    tokens_output: int = 0
    # Cache breakdown — subset of tokens_input. Lets the cost estimator apply
    # the right rate (reads 10%, writes 125%).
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    summary: str = ""  # Short human-readable description for the decisions log
    # Optional per-outcome detail:
    rework_notes: str = ""  # When NEEDS_REWORK: what to change
    block_reason: str = ""  # When BLOCKED: why, for the user to see
    failure_reason: str = ""  # When FAILED: error details for logging
    # When NEEDS_USER_REVIEW: what the user should check and how to run it
    review_checklist: list[str] = field(default_factory=list)
    review_run_command: str = ""  # e.g. "python -m http.server 8000 and open localhost:8000"
    review_files_to_check: list[str] = field(default_factory=list)


@dataclass
class TaskContext:
    """Everything a TaskRunner needs to execute one task."""

    task: dict[str, Any]  # The task row from tasks.json
    store: ProjectStore
    sandbox: SandboxExecutor
    project_token_budget_remaining: int
    task_token_budget: int
    # Free-form scratchpad for the runner to return extra info if needed
    extras: dict[str, Any] = field(default_factory=dict)


class TaskRunner(Protocol):
    """Executes a single task and yields streaming events. Final event's payload contains
    the TaskOutcome.
    """

    async def run(self, ctx: TaskContext) -> AsyncIterator[StreamEvent]:  # type: ignore[misc]
        ...
