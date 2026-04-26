"""Project state store — manages `.devteam/` inside a user's project directory.

All state is file-based so it survives crashes, can be inspected by the user, and is easy to
debug. The orchestrator and agents read and write through this store rather than touching
files directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import aiofiles


class ProjectStatus(str, Enum):
    INIT = "init"
    INTERVIEW = "interview"
    PLANNING = "planning"
    AWAIT_APPROVAL = "await_approval"
    DISPATCHING = "dispatching"
    EXECUTING = "executing"
    AWAITING_TASK_REVIEW = "awaiting_task_review"  # Coder finished a UI task; user must verify
    PHASE_REVIEW = "phase_review"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ProjectPhase:
    id: str  # e.g. "P1"
    title: str
    status: str = "pending"  # pending | active | done
    approved_by_user: bool = False


@dataclass
class ProjectMeta:
    id: str
    name: str
    root_path: str
    created_at: float
    status: ProjectStatus = ProjectStatus.INIT
    current_phase: str | None = None
    phases: list[ProjectPhase] = field(default_factory=list)

    # Budgets — set during setup, enforced by orchestrator. These defaults
    # only apply if meta.json is missing a field (old projects before we
    # started writing them). New projects get values from config.Settings.
    project_token_budget: int = 5_000_000
    default_task_token_budget: int = 150_000
    max_task_iterations: int = 8
    max_wall_clock_seconds: int | None = None  # None = run until done

    # Running totals
    tokens_used: int = 0  # Legacy aggregate (input + output, all models combined)
    tasks_completed: int = 0

    # Per-model, per-direction token accounting for live cost display.
    # Split this way because Opus and Sonnet have very different prices.
    # tokens_input_* counts ALL input tokens including cache reads and cache
    # creation; the cache-specific fields further break that down so the cost
    # estimator can apply the right rate (cache reads are 10% of input price,
    # cache writes are 125%).
    tokens_input_opus: int = 0
    tokens_output_opus: int = 0
    tokens_input_sonnet: int = 0
    tokens_output_sonnet: int = 0
    tokens_input_haiku: int = 0
    tokens_output_haiku: int = 0

    # Cache breakdown — subset of the tokens_input_* above. Lets the cost
    # estimator distinguish full-price input from discounted cache reads and
    # surcharged cache writes.
    cache_read_opus: int = 0
    cache_creation_opus: int = 0
    cache_read_sonnet: int = 0
    cache_creation_sonnet: int = 0
    cache_read_haiku: int = 0
    cache_creation_haiku: int = 0

    # The OS the user is on — baked into Coder/Reviewer prompts so commands
    # they hand to the user (review_run_command, task notes) use the right
    # syntax. Values: "windows", "macos", "linux". Defaults to "linux" for
    # old projects that predate this field; the recovery code in projects.py
    # auto-fills it from the current backend host the first time such a
    # project is accessed. NEW projects are initialized from the backend's
    # detected platform at creation time.
    user_platform: str = "linux"

    # Per-agent model overrides. None means "use the global default from
    # config.Settings"; a string here overrides for this project only.
    #
    # The orchestrator reads these via _model_for_role(meta, role) before
    # invoking each agent. Old projects without these fields fall through
    # to None → global default, no migration needed.
    #
    # Validated against MODEL_CHOICES at API write time, so by the time
    # the orchestrator reads them they're guaranteed to be a known string
    # or None.
    model_architect: str | None = None
    model_dispatcher: str | None = None
    model_coder: str | None = None
    model_reviewer: str | None = None

    # Browser-based runtime verification toggle. When True, the Reviewer's
    # Rule 3 verification (runtime checks for runnable artifacts) uses the
    # playwright_check tool — actually opens the page in a headless Chromium
    # and captures screenshot + console errors + DOM state. When False
    # (default), Rule 3 falls back to lighter-weight node/curl checks.
    #
    # Off by default because Playwright requires a one-time ~150MB browser
    # download on first use; opting in is explicit. User can toggle mid-
    # project; the Reviewer reads this flag at the start of every review.
    playwright_enabled: bool = False


@dataclass
class InboxMessage:
    id: str
    from_role: str
    to_role: str
    subject: str
    body: str
    created_at: float
    read: bool = False


class ProjectStore:
    """File-backed state for a single project."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()
        self.devteam_dir = self.root / ".devteam"
        self.plan_path = self.devteam_dir / "plan.md"
        self.tasks_path = self.devteam_dir / "tasks.json"
        self.meta_path = self.devteam_dir / "meta.json"
        self.decisions_path = self.devteam_dir / "decisions.log"
        self.agent_log_path = self.devteam_dir / "agent_log.jsonl"
        self.inboxes_dir = self.devteam_dir / "inboxes"
        self.interview_path = self.devteam_dir / "interview.jsonl"

    # --- Initialization ------------------------------------------------------------------------

    def init(self, *, project_id: str, name: str) -> ProjectMeta:
        """Create `.devteam/` and seed initial files. Idempotent."""
        self.devteam_dir.mkdir(parents=True, exist_ok=True)
        self.inboxes_dir.mkdir(parents=True, exist_ok=True)
        for role in ("architect", "dispatcher", "coder", "reviewer", "user"):
            inbox = self.inboxes_dir / f"{role}.json"
            if not inbox.exists():
                _write_text(inbox, "[]")
        if not self.decisions_path.exists():
            _write_text(self.decisions_path, "")
        if not self.agent_log_path.exists():
            _write_text(self.agent_log_path, "")
        if not self.interview_path.exists():
            _write_text(self.interview_path, "")
        if not self.meta_path.exists():
            meta = ProjectMeta(
                id=project_id, name=name, root_path=str(self.root), created_at=time.time()
            )
            self._write_meta(meta)
            return meta
        return self.read_meta()

    # --- Meta ----------------------------------------------------------------------------------

    def read_meta(self) -> ProjectMeta:
        data = json.loads(_read_text(self.meta_path))
        phases = [ProjectPhase(**p) for p in data.pop("phases", [])]
        status = ProjectStatus(data.pop("status", "init"))
        return ProjectMeta(status=status, phases=phases, **data)

    def write_meta(self, meta: ProjectMeta) -> None:
        self._write_meta(meta)

    def add_token_usage(
        self,
        model: str,
        tokens_input: int,
        tokens_output: int,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        """Atomically accumulate token usage bucketed by model family, for cost display.

        Classifies by substring: any model string containing 'opus' goes in the opus
        bucket, 'sonnet' in sonnet, 'haiku' in haiku. Unknown models fall through to
        the aggregate tokens_used only. Also updates the legacy aggregate so older
        UI code keeps working.

        cache_read and cache_creation are subsets of tokens_input — they're the portion
        of that input that was served from cache (10% price) or written to cache (125%
        price). Default to 0 for callers that don't yet pass them.
        """
        meta = self.read_meta()
        meta.tokens_used += tokens_input + tokens_output
        m = (model or "").lower()
        if "opus" in m:
            meta.tokens_input_opus += tokens_input
            meta.tokens_output_opus += tokens_output
            meta.cache_read_opus += cache_read
            meta.cache_creation_opus += cache_creation
        elif "sonnet" in m:
            meta.tokens_input_sonnet += tokens_input
            meta.tokens_output_sonnet += tokens_output
            meta.cache_read_sonnet += cache_read
            meta.cache_creation_sonnet += cache_creation
        elif "haiku" in m:
            meta.tokens_input_haiku += tokens_input
            meta.tokens_output_haiku += tokens_output
            meta.cache_read_haiku += cache_read
            meta.cache_creation_haiku += cache_creation
        self._write_meta(meta)

    def _write_meta(self, meta: ProjectMeta) -> None:
        data = asdict(meta)
        data["status"] = meta.status.value
        _write_text(self.meta_path, json.dumps(data, indent=2))

    # --- Plan ----------------------------------------------------------------------------------

    def read_plan(self) -> str:
        if not self.plan_path.exists():
            return ""
        return _read_text(self.plan_path)

    def write_plan(self, content: str) -> None:
        _write_text(self.plan_path, content)

    # --- Tasks ---------------------------------------------------------------------------------

    def read_tasks(self) -> list[dict[str, Any]]:
        if not self.tasks_path.exists():
            return []
        return json.loads(_read_text(self.tasks_path))

    def write_tasks(self, tasks: list[dict[str, Any]]) -> None:
        _write_text(self.tasks_path, json.dumps(tasks, indent=2))

    def update_task(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        tasks = self.read_tasks()
        for t in tasks:
            if t["id"] == task_id:
                t.update(updates)
                self.write_tasks(tasks)
                return t
        return None

    # --- Decisions log -------------------------------------------------------------------------

    async def append_decision(self, entry: dict[str, Any]) -> None:
        entry_with_ts = {"timestamp": time.time(), **entry}
        async with aiofiles.open(self.decisions_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(entry_with_ts) + "\n")

    def append_decision_sync(self, entry: dict[str, Any]) -> None:
        entry_with_ts = {"timestamp": time.time(), **entry}
        with self.decisions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry_with_ts) + "\n")

    def read_decisions(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.decisions_path.exists():
            return []
        lines = _read_text(self.decisions_path).strip().splitlines()
        if limit is not None:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line]

    # --- Agent log (streaming activity) --------------------------------------------------------

    async def append_agent_log(self, entry: dict[str, Any]) -> None:
        entry_with_ts = {"timestamp": time.time(), **entry}
        async with aiofiles.open(self.agent_log_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(entry_with_ts) + "\n")

    # --- Interview log (just architect <-> user messages) --------------------------------------

    async def append_interview(self, role: str, content: str) -> None:
        entry = {"timestamp": time.time(), "role": role, "content": content}
        async with aiofiles.open(self.interview_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(entry) + "\n")

    def read_interview(self) -> list[dict[str, Any]]:
        if not self.interview_path.exists():
            return []
        return [
            json.loads(line)
            for line in _read_text(self.interview_path).splitlines()
            if line
        ]

    # --- Inboxes -------------------------------------------------------------------------------

    def read_inbox(self, role: str, *, unread_only: bool = False) -> list[InboxMessage]:
        path = self.inboxes_dir / f"{role}.json"
        if not path.exists():
            return []
        data = json.loads(_read_text(path))
        messages = [InboxMessage(**m) for m in data]
        if unread_only:
            messages = [m for m in messages if not m.read]
        return messages

    def append_inbox(
        self, *, to_role: str, from_role: str, subject: str, body: str
    ) -> InboxMessage:
        path = self.inboxes_dir / f"{to_role}.json"
        messages = self.read_inbox(to_role)
        msg = InboxMessage(
            id=f"msg_{int(time.time() * 1000)}_{len(messages)}",
            from_role=from_role,
            to_role=to_role,
            subject=subject,
            body=body,
            created_at=time.time(),
        )
        messages.append(msg)
        _write_text(path, json.dumps([asdict(m) for m in messages], indent=2))
        return msg

    def mark_inbox_read(self, role: str, message_ids: Iterable[str]) -> None:
        path = self.inboxes_dir / f"{role}.json"
        messages = self.read_inbox(role)
        ids = set(message_ids)
        for m in messages:
            if m.id in ids:
                m.read = True
        _write_text(path, json.dumps([asdict(m) for m in messages], indent=2))


# --- UTF-8 helpers ---------------------------------------------------------------------------
#
# Every disk read/write goes through these. On Windows, Path.write_text() defaults to the
# system's legacy ANSI codepage (cp1252), which can't encode em-dashes, smart quotes, emoji,
# or anything else outside Latin-1. That's what caused the write_plan crash the first time
# around. Forcing UTF-8 everywhere is the fix.


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
