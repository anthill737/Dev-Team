"""Per-project agent event buffer.

Captures the stream of events produced by each agent during a project's
lifetime so the frontend's Agent Inspector can render a chronological
transcript per agent (Architect, Dispatcher, Coder, Reviewer).

Design notes:
  - In-memory only. Buffers are cleared on backend restart. Long-term audit
    history lives in decisions.log; this buffer is for live UI consumption,
    not durable storage.
  - Per-project, per-agent ring with a hard cap. We don't want a runaway
    Coder loop to fill RAM with text deltas.
  - Each event has a monotonic `seq` so the frontend can poll with a
    `since=<seq>` parameter and only get new events.
  - Thread/async safety: backend is asyncio single-threaded so a plain dict
    is fine. Locks not required.

Event shape:
  {
    "agent": "architect" | "dispatcher" | "coder" | "reviewer",
    "kind": str,             # the StreamEvent.kind
    "payload": dict[str, Any],  # raw StreamEvent.payload
    "timestamp": float,      # unix seconds when event was captured
    "seq": int,              # monotonic per-project sequence
    "task_id": str | None,   # the task this event applies to (Coder/Reviewer)
  }

The captured events overlap with decisions.log content but at finer
granularity — text_delta, tool_use_start, tool_result, etc., not just
the higher-level "rationale" / "tool_call_complete" entries the decision
log records.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Hard cap on events per (project, agent) pair. A single Coder run on a
# complex task can emit thousands of text_deltas and tool events; we keep the
# most recent N. The frontend polls and renders fresh, so missing the very
# beginning of a long-running task is acceptable; missing the recent activity
# is not.
_MAX_EVENTS_PER_AGENT = 2000

# All known agent roles. Used to initialize buffers and validate `agent`
# query parameters from the frontend.
_KNOWN_AGENTS = ("architect", "dispatcher", "coder", "reviewer", "orchestrator")


class AgentEventBuffer:
    """Per-project ring buffer of agent events keyed by agent role.

    The orchestrator and execution loop call `record(project_id, agent, event)`
    as events stream past. The HTTP API calls `fetch(project_id, agent, since)`
    to serve events to the frontend. `clear(project_id)` is used when a project
    is deleted.
    """

    def __init__(self) -> None:
        # project_id -> agent_role -> deque of event dicts
        # defaultdict keeps the access path clean — first record() for a new
        # project creates the inner dict + deques on demand.
        self._buffers: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(
            lambda: {agent: deque(maxlen=_MAX_EVENTS_PER_AGENT) for agent in _KNOWN_AGENTS}
        )
        # Monotonic per-project sequence counter. Frontend polls with
        # `since=<seq>` and we return events with `seq > since`.
        self._seq_counters: dict[str, int] = defaultdict(int)

    def record(
        self,
        project_id: str,
        agent: str,
        kind: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> None:
        """Append an event to the buffer for (project_id, agent).

        Silently ignores unknown agent roles to avoid breaking on a typo —
        the buffer's job is to be lossy when in doubt rather than crash a
        live agent stream.
        """
        if agent not in _KNOWN_AGENTS:
            logger.debug("Ignoring event for unknown agent role %r", agent)
            return
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

    def fetch(
        self,
        project_id: str,
        agent: str | None = None,
        since: int = 0,
    ) -> list[dict[str, Any]]:
        """Return events with seq > since.

        If `agent` is given, restrict to that agent's events. If None, return
        all agents' events merged in seq order (caller can group them client-
        side). Limit is the buffer cap; we don't paginate beyond that.
        """
        if project_id not in self._buffers:
            return []
        per_agent = self._buffers[project_id]
        if agent is not None:
            if agent not in _KNOWN_AGENTS:
                return []
            return [e for e in per_agent[agent] if e["seq"] > since]
        # Merge all agents and sort by seq. seq is monotonic so this is
        # equivalent to chronological order.
        merged: list[dict[str, Any]] = []
        for events in per_agent.values():
            merged.extend(e for e in events if e["seq"] > since)
        merged.sort(key=lambda e: e["seq"])
        return merged

    def latest_seq(self, project_id: str) -> int:
        """Highest sequence number recorded for this project. Frontend uses
        this on first load to request `since=<seq>` for tail-only updates."""
        return self._seq_counters.get(project_id, 0)

    def agent_summary(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Per-agent summary suitable for the tab strip header.

        Returns a dict mapping agent role to {event_count, last_seq, last_ts,
        last_kind}. Frontend uses this to show activity dots / counts on each
        tab without having to fetch all events.
        """
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
        """Drop all buffered events for a project. Called when the project
        is deleted from the registry."""
        self._buffers.pop(project_id, None)
        self._seq_counters.pop(project_id, None)

    def record_many(
        self,
        project_id: str,
        agent: str,
        events: Iterable[tuple[str, dict[str, Any]]],
        task_id: str | None = None,
    ) -> None:
        """Bulk-record (kind, payload) pairs. Convenience for tests and any
        code path that has a batch of events to commit at once."""
        for kind, payload in events:
            self.record(project_id, agent, kind, payload, task_id=task_id)


def _empty_summary() -> dict[str, Any]:
    return {
        "event_count": 0,
        "last_seq": 0,
        "last_ts": 0.0,
        "last_kind": None,
    }


# Process-global singleton. Safe because the backend is a single asyncio
# event loop; no thread/process concurrency issues.
_GLOBAL_BUFFER: AgentEventBuffer | None = None


def get_buffer() -> AgentEventBuffer:
    """Return the shared AgentEventBuffer. Lazy-initialized on first call."""
    global _GLOBAL_BUFFER
    if _GLOBAL_BUFFER is None:
        _GLOBAL_BUFFER = AgentEventBuffer()
    return _GLOBAL_BUFFER


def reset_buffer() -> None:
    """Reset the global buffer. Tests only — production never calls this."""
    global _GLOBAL_BUFFER
    _GLOBAL_BUFFER = None
