"""Per-project agent event buffer with disk persistence.

Captures the stream of events produced by each agent so the frontend's Agent
Inspector can render a chronological transcript per agent (Architect, Dispatcher,
Coder, Reviewer, Orchestrator).

Persistence model:
  - Per-project, per-agent JSONL files at:
      <project_root>/.devteam/agent_events/{role}.jsonl
  - One JSON event per line, append-only. Append-only is crash-safe — a
    process killed mid-write loses at most a single partial line at the tail,
    which we tolerate during read by ignoring unparseable trailing lines.
  - Reads use an in-memory cache hydrated lazily on first access per project.
    The cache holds up to _MAX_EVENTS_PER_AGENT recent events per role; older
    events stay on disk but aren't served. The Inspector shows recent activity;
    full audit trail lives in decisions.log.

Event shape:
  {
    "agent": "architect" | "dispatcher" | "coder" | "reviewer" | "orchestrator",
    "kind": str,                # the StreamEvent.kind
    "payload": dict[str, Any],  # raw StreamEvent.payload (JSON-safe)
    "timestamp": float,         # unix seconds when event was captured
    "seq": int,                 # monotonic per-project sequence
    "task_id": str | None,      # the task this event applies to (Coder/Reviewer)
  }

Concurrency:
  - Backend is asyncio single-threaded. No locks needed.
  - Multiple frontend tabs polling the same project share the in-memory cache.
  - Two backend processes against the same project root would interleave at
    line boundaries (safe for JSONL) but would conflict on seq numbers.
    Documented as "don't run two backends on one project."
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# Max events held in memory per (project, agent). Older events stay on disk
# but aren't served by fetch(). Sized for the Inspector UI: 2000 covers
# multiple long Coder runs per agent role per project.
_MAX_EVENTS_PER_AGENT = 2000

# All known agent roles. Used to initialize buffers and validate `agent`
# query params from the frontend.
_KNOWN_AGENTS = ("architect", "dispatcher", "coder", "reviewer", "orchestrator")

# Subdirectory under .devteam/ for the JSONL files.
_EVENTS_SUBDIR = "agent_events"

# Type alias for project_id → root path lookup. Injected so the buffer
# doesn't have a hard dep on the registry module (testability).
ProjectRootResolver = Callable[[str], "Path | None"]


class AgentEventBuffer:
    """Per-project agent event buffer with disk persistence.

    Public API matches the prior in-memory-only version. Internally writes
    each event to a JSONL file under <project_root>/.devteam/agent_events/
    and lazily hydrates the in-memory cache on first access.

    Construction takes a project_root_resolver — a callable mapping project_id
    to root path. If the resolver returns None for a project_id (registry
    missing, project deleted), persistence falls through to memory-only mode
    for that project. Logged at debug; not an error.
    """

    def __init__(self, project_root_resolver: ProjectRootResolver | None = None) -> None:
        # project_id -> agent_role -> deque of event dicts (in-memory cache)
        self._buffers: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(
            lambda: {agent: deque(maxlen=_MAX_EVENTS_PER_AGENT) for agent in _KNOWN_AGENTS}
        )
        # Monotonic per-project sequence counter. After hydration this matches
        # the highest seq seen across all of the project's JSONL files.
        self._seq_counters: dict[str, int] = defaultdict(int)
        # Set of project_ids whose disk files have been hydrated. First
        # access for a project triggers hydration; subsequent accesses are
        # pure-memory.
        self._hydrated: set[str] = set()
        # None resolver = memory-only mode; buffer still works, just not durable.
        self._resolver = project_root_resolver

    # --- public API ---------------------------------------------------------

    def record(
        self,
        project_id: str,
        agent: str,
        kind: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> None:
        """Append an event for (project_id, agent). Persists to disk if a
        resolver is configured and the project is registered.

        Silently ignores unknown agent roles. Persistence failures (disk full,
        permission denied, etc.) are logged but never raised — the orchestrator
        live stream must not be broken by storage hiccups.
        """
        if agent not in _KNOWN_AGENTS:
            logger.debug("Ignoring event for unknown agent role %r", agent)
            return
        self._ensure_hydrated(project_id)
        self._seq_counters[project_id] += 1
        event = {
            "agent": agent,
            "kind": kind,
            "payload": payload,
            "timestamp": time.time(),
            "seq": self._seq_counters[project_id],
            "task_id": task_id,
        }
        self._buffers[project_id][agent].append(event)
        self._persist_event(project_id, agent, event)

    def fetch(
        self,
        project_id: str,
        agent: str | None = None,
        since: int = 0,
    ) -> list[dict[str, Any]]:
        """Return events with seq > since. Optionally filter to one agent.

        Hydrates from disk on first access. Subsequent calls hit memory only.
        """
        self._ensure_hydrated(project_id)
        if project_id not in self._buffers:
            return []
        per_agent = self._buffers[project_id]
        if agent is not None:
            if agent not in _KNOWN_AGENTS:
                return []
            return [e for e in per_agent[agent] if e["seq"] > since]
        # Merge all agents in seq order. seq is monotonic so this is
        # equivalent to chronological order.
        merged: list[dict[str, Any]] = []
        for events in per_agent.values():
            merged.extend(e for e in events if e["seq"] > since)
        merged.sort(key=lambda e: e["seq"])
        return merged

    def latest_seq(self, project_id: str) -> int:
        """Highest seq for this project. Frontend uses for first-load tail
        request and as a cheap activity indicator."""
        self._ensure_hydrated(project_id)
        return self._seq_counters.get(project_id, 0)

    def agent_summary(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Per-agent summary for the inspector tab strip header."""
        self._ensure_hydrated(project_id)
        if project_id not in self._buffers:
            return {agent: _empty_summary() for agent in _KNOWN_AGENTS}
        per_agent = self._buffers[project_id]
        summary: dict[str, dict[str, Any]] = {}
        for agent in _KNOWN_AGENTS:
            events = per_agent[agent]
            if not events:
                summary[agent] = _empty_summary()
            else:
                last = events[-1]
                summary[agent] = {
                    "event_count": len(events),
                    "last_seq": last["seq"],
                    "last_ts": last["timestamp"],
                    "last_kind": last["kind"],
                }
        return summary

    def clear(self, project_id: str) -> None:
        """Drop all in-memory + on-disk events for a project. Called when
        the project is deleted from the registry."""
        self._buffers.pop(project_id, None)
        self._seq_counters.pop(project_id, None)
        self._hydrated.discard(project_id)
        events_dir = self._events_dir(project_id)
        if events_dir is None:
            return
        try:
            for jsonl in events_dir.glob("*.jsonl"):
                jsonl.unlink()
            try:
                events_dir.rmdir()
            except OSError:
                pass  # not empty or already gone — fine
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to remove agent_events files for %s",
                project_id,
                exc_info=True,
            )

    def record_many(
        self,
        project_id: str,
        agent: str,
        events: Iterable[tuple[str, dict[str, Any]]],
        task_id: str | None = None,
    ) -> None:
        """Bulk record. Convenience for tests + batch paths."""
        for kind, payload in events:
            self.record(project_id, agent, kind, payload, task_id=task_id)

    # --- persistence internals ----------------------------------------------

    def _events_dir(self, project_id: str) -> Path | None:
        """Resolve the on-disk directory for this project's JSONL files.

        None means "memory-only mode" for this project — caller skips persistence.
        """
        if self._resolver is None:
            return None
        try:
            root = self._resolver(project_id)
        except Exception:  # noqa: BLE001
            logger.debug("Project root resolver raised for %s", project_id, exc_info=True)
            return None
        if root is None:
            return None
        return root / ".devteam" / _EVENTS_SUBDIR

    def _ensure_hydrated(self, project_id: str) -> None:
        """Load on-disk events into the in-memory cache. Runs once per project
        per process lifetime; subsequent calls are no-ops.

        Tolerates malformed lines (logs and skips) — append-only writes can
        leave a partial trailing line if the process was killed mid-write.
        """
        if project_id in self._hydrated:
            return
        # Mark hydrated immediately to prevent repeated FS checks even when
        # there's nothing on disk for this project.
        self._hydrated.add(project_id)

        events_dir = self._events_dir(project_id)
        if events_dir is None or not events_dir.exists():
            return

        # Force-instantiate the per-project buffer so reads return correct
        # empty structure even if a role file is missing.
        _ = self._buffers[project_id]

        max_seq = 0
        for role in _KNOWN_AGENTS:
            jsonl_path = events_dir / f"{role}.jsonl"
            if not jsonl_path.exists():
                continue
            for ev in self._read_jsonl(jsonl_path):
                # Defensive: trust file path over event's claimed agent if
                # they disagree. Mismatches indicate corruption or manual
                # edits; route the event to where it was filed and don't
                # lose it.
                if ev.get("agent") != role:
                    logger.debug(
                        "Event in %s claims agent=%r; routing to %s anyway",
                        jsonl_path, ev.get("agent"), role,
                    )
                    ev["agent"] = role
                self._buffers[project_id][role].append(ev)
                seq = ev.get("seq", 0)
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
        self._seq_counters[project_id] = max_seq

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        """Read JSONL file. Tolerates malformed/partial trailing lines."""
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for lineno, raw in enumerate(f, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        events.append(json.loads(raw))
                    except json.JSONDecodeError:
                        # Likely a partial line from a crash mid-write. One
                        # bad trailing line is normal; many bad lines would
                        # indicate real corruption — debug-level either way,
                        # the caller decides whether to escalate.
                        logger.debug(
                            "Skipping malformed line %d in %s", lineno, path
                        )
        except OSError:
            logger.debug("Failed to read %s", path, exc_info=True)
        return events

    def _persist_event(
        self, project_id: str, agent: str, event: dict[str, Any]
    ) -> None:
        """Append a single event to the role's JSONL file. Best-effort —
        catches all exceptions and logs them at debug."""
        events_dir = self._events_dir(project_id)
        if events_dir is None:
            return
        try:
            events_dir.mkdir(parents=True, exist_ok=True)
            jsonl_path = events_dir / f"{agent}.jsonl"
            # One compact JSON object per line. Open/append/close per call —
            # OS page cache batches writes; per-project open handles would be
            # a micro-optimization for later.
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to persist agent event for project=%s agent=%s",
                project_id,
                agent,
                exc_info=True,
            )


def _empty_summary() -> dict[str, Any]:
    return {
        "event_count": 0,
        "last_seq": 0,
        "last_ts": 0.0,
        "last_kind": None,
    }


# Process-global singleton. Lazy init so tests can reset it for fresh state.
_GLOBAL_BUFFER: AgentEventBuffer | None = None


def get_buffer() -> AgentEventBuffer:
    """Return the shared AgentEventBuffer.

    First access constructs it with a resolver that reads the projects
    registry. The resolver lookup is lazy and tolerates failures — the
    buffer module doesn't import-depend on the API layer.
    """
    global _GLOBAL_BUFFER
    if _GLOBAL_BUFFER is None:
        _GLOBAL_BUFFER = AgentEventBuffer(project_root_resolver=_default_resolver)
    return _GLOBAL_BUFFER


def reset_buffer() -> None:
    """Reset the global buffer. Tests only.

    A fresh buffer means hydration runs again on next access — what tests
    want when they create per-test temporary projects.
    """
    global _GLOBAL_BUFFER
    _GLOBAL_BUFFER = None


def _default_resolver(project_id: str) -> Path | None:
    """Resolve project_id → root path via the registry."""
    try:
        # Local import: avoids top-level cycle (api.projects → buffer).
        from ..api.projects import _load_registry

        for entry in _load_registry():
            if entry.get("id") == project_id:
                root = entry.get("root_path")
                if root:
                    return Path(root)
        return None
    except Exception:  # noqa: BLE001
        logger.debug(
            "Default resolver failed for %s; using memory-only mode",
            project_id,
            exc_info=True,
        )
        return None
