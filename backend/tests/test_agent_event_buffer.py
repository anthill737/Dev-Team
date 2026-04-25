"""Tests for AgentEventBuffer."""

from __future__ import annotations

import pytest

from app.orchestrator.agent_event_buffer import (
    AgentEventBuffer,
    _MAX_EVENTS_PER_AGENT,
    get_buffer,
    reset_buffer,
)


def test_record_and_fetch_for_one_agent() -> None:
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "hello"})
    buf.record("p1", "architect", "text_delta", {"text": " world"})

    events = buf.fetch("p1", agent="architect")
    assert len(events) == 2
    assert events[0]["payload"]["text"] == "hello"
    assert events[1]["payload"]["text"] == " world"
    # Seq is monotonic
    assert events[1]["seq"] > events[0]["seq"]


def test_fetch_with_since_returns_only_new_events() -> None:
    """The since parameter is the polling primitive — frontend tracks the
    highest seq it's seen and asks for events newer than that."""
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "first"})
    buf.record("p1", "architect", "text_delta", {"text": "second"})
    buf.record("p1", "architect", "text_delta", {"text": "third"})

    all_events = buf.fetch("p1", agent="architect")
    second_seq = all_events[1]["seq"]
    after_second = buf.fetch("p1", agent="architect", since=second_seq)
    assert len(after_second) == 1
    assert after_second[0]["payload"]["text"] == "third"


def test_fetch_without_agent_merges_all_agents_in_seq_order() -> None:
    """Frontend sometimes wants the raw chronological log across all agents
    (e.g., a 'firehose' view). seq order is what determines that."""
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "a-1"})
    buf.record("p1", "coder", "tool_use_start", {"name": "bash"})
    buf.record("p1", "architect", "text_delta", {"text": "a-2"})
    buf.record("p1", "reviewer", "text_delta", {"text": "r-1"})

    merged = buf.fetch("p1")
    assert len(merged) == 4
    # Verify order is chronological (matches insert order via seq)
    seqs = [e["seq"] for e in merged]
    assert seqs == sorted(seqs)
    # Per-agent extraction still works
    coder_events = [e for e in merged if e["agent"] == "coder"]
    assert len(coder_events) == 1


def test_unknown_agent_is_silently_ignored_on_record() -> None:
    """Don't blow up an agent stream because of a typo — ignore the event."""
    buf = AgentEventBuffer()
    buf.record("p1", "fake_agent", "text_delta", {"text": "x"})
    assert buf.fetch("p1") == []


def test_unknown_agent_returns_empty_on_fetch() -> None:
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "x"})
    # Asking for events from a role we don't recognize → empty, not crash
    assert buf.fetch("p1", agent="not_real") == []


def test_buffer_caps_at_max_events_per_agent() -> None:
    """A long-running Coder loop must not blow RAM. The deque drops oldest."""
    buf = AgentEventBuffer()
    for i in range(_MAX_EVENTS_PER_AGENT + 100):
        buf.record("p1", "coder", "text_delta", {"text": f"chunk-{i}"})
    events = buf.fetch("p1", agent="coder")
    assert len(events) == _MAX_EVENTS_PER_AGENT
    # The oldest 100 should have been evicted
    assert events[0]["payload"]["text"] != "chunk-0"
    assert events[-1]["payload"]["text"] == f"chunk-{_MAX_EVENTS_PER_AGENT + 99}"


def test_agent_summary_when_empty() -> None:
    buf = AgentEventBuffer()
    summary = buf.agent_summary("p1")
    # All known agents present with zero counts
    for role in ("architect", "dispatcher", "coder", "reviewer", "orchestrator"):
        assert summary[role]["event_count"] == 0
        assert summary[role]["last_seq"] == 0
        assert summary[role]["last_kind"] is None


def test_agent_summary_with_events() -> None:
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "x"})
    buf.record("p1", "architect", "tool_use_start", {"name": "write_plan"})
    buf.record("p1", "coder", "text_delta", {"text": "y"})

    summary = buf.agent_summary("p1")
    assert summary["architect"]["event_count"] == 2
    assert summary["architect"]["last_kind"] == "tool_use_start"
    assert summary["coder"]["event_count"] == 1
    assert summary["dispatcher"]["event_count"] == 0  # no events recorded


def test_clear_removes_project_buffers() -> None:
    """When a project is deleted, its in-memory buffers should be reclaimed."""
    buf = AgentEventBuffer()
    buf.record("p1", "architect", "text_delta", {"text": "x"})
    buf.record("p2", "coder", "tool_use_start", {"name": "bash"})

    buf.clear("p1")

    assert buf.fetch("p1") == []
    # p2 untouched
    assert len(buf.fetch("p2")) == 1


def test_record_with_task_id() -> None:
    """task_id is optional metadata that lets the frontend group Coder events
    by which task they belong to."""
    buf = AgentEventBuffer()
    buf.record("p1", "coder", "tool_use_start", {"name": "bash"}, task_id="P1-T1")
    events = buf.fetch("p1", agent="coder")
    assert events[0]["task_id"] == "P1-T1"


def test_record_many_bulk() -> None:
    buf = AgentEventBuffer()
    buf.record_many(
        "p1",
        "architect",
        [
            ("text_delta", {"text": "a"}),
            ("text_delta", {"text": "b"}),
        ],
    )
    events = buf.fetch("p1", agent="architect")
    assert len(events) == 2


def test_get_buffer_returns_singleton() -> None:
    reset_buffer()
    b1 = get_buffer()
    b2 = get_buffer()
    assert b1 is b2
    reset_buffer()


def test_latest_seq_reflects_record_count() -> None:
    buf = AgentEventBuffer()
    assert buf.latest_seq("p1") == 0
    buf.record("p1", "architect", "text_delta", {"text": "x"})
    buf.record("p1", "architect", "text_delta", {"text": "y"})
    assert buf.latest_seq("p1") == 2


# --- Persistence tests ----------------------------------------------------------

import tempfile
import json
from pathlib import Path


def test_persistence_round_trip_via_resolver() -> None:
    """Events recorded with a resolver configured should land on disk and
    survive a fresh AgentEventBuffer instance."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        # Manual resolver — returns our temp dir for any project_id
        resolver = lambda pid: project_root  # noqa: E731

        buf1 = AgentEventBuffer(project_root_resolver=resolver)
        buf1.record("p1", "architect", "text_delta", {"text": "hello"})
        buf1.record("p1", "architect", "text_delta", {"text": " world"})
        buf1.record("p1", "coder", "tool_use_start", {"name": "bash"})

        # On disk?
        events_dir = project_root / ".devteam" / "agent_events"
        assert events_dir.exists()
        arch_file = events_dir / "architect.jsonl"
        coder_file = events_dir / "coder.jsonl"
        assert arch_file.exists()
        assert coder_file.exists()
        # Two architect events, one coder event
        assert len(arch_file.read_text().strip().split("\n")) == 2
        assert len(coder_file.read_text().strip().split("\n")) == 1

        # Fresh buffer with same resolver → events come back via hydration
        buf2 = AgentEventBuffer(project_root_resolver=resolver)
        events = buf2.fetch("p1", agent="architect")
        assert len(events) == 2
        assert events[0]["payload"]["text"] == "hello"
        # seq counter restored from max seq across files
        assert buf2.latest_seq("p1") == 3


def test_persistence_no_resolver_is_memory_only() -> None:
    """Constructing with no resolver works but doesn't write to disk."""
    buf = AgentEventBuffer(project_root_resolver=None)
    buf.record("p1", "architect", "text_delta", {"text": "x"})
    assert len(buf.fetch("p1")) == 1
    # Nothing on disk because we have nowhere to write
    # (verified implicitly — there's no path to check)


def test_persistence_resolver_returns_none_is_memory_only() -> None:
    """Resolver returning None for a given project_id should fall back to
    memory-only mode for that project. No disk I/O, no errors."""
    buf = AgentEventBuffer(project_root_resolver=lambda pid: None)
    buf.record("missing_proj", "architect", "text_delta", {"text": "x"})
    assert len(buf.fetch("missing_proj")) == 1


def test_persistence_tolerates_malformed_trailing_line() -> None:
    """A partial trailing line (from a process killed mid-write) should be
    skipped on hydration. Leading and middle valid events should still load."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        events_dir = project_root / ".devteam" / "agent_events"
        events_dir.mkdir(parents=True)
        arch_file = events_dir / "architect.jsonl"
        # Two valid events + one malformed trailing line
        valid_events = [
            {"agent": "architect", "kind": "text_delta", "payload": {"text": "a"},
             "timestamp": 1.0, "seq": 1, "task_id": None},
            {"agent": "architect", "kind": "text_delta", "payload": {"text": "b"},
             "timestamp": 2.0, "seq": 2, "task_id": None},
        ]
        arch_file.write_text(
            "\n".join(json.dumps(e) for e in valid_events) + "\n{partial: malformed"
        )

        resolver = lambda pid: project_root  # noqa: E731
        buf = AgentEventBuffer(project_root_resolver=resolver)
        events = buf.fetch("p1", agent="architect")
        assert len(events) == 2
        assert events[1]["payload"]["text"] == "b"


def test_persistence_clear_removes_files() -> None:
    """clear() should delete all on-disk events for a project."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        resolver = lambda pid: project_root  # noqa: E731

        buf = AgentEventBuffer(project_root_resolver=resolver)
        buf.record("p1", "architect", "text_delta", {"text": "x"})
        events_dir = project_root / ".devteam" / "agent_events"
        assert (events_dir / "architect.jsonl").exists()

        buf.clear("p1")

        # File and directory gone
        assert not (events_dir / "architect.jsonl").exists()
        # In-memory cleared too
        assert buf.fetch("p1") == []
        assert buf.latest_seq("p1") == 0


def test_persistence_seq_counter_restored_correctly_across_agents() -> None:
    """The seq counter on hydrate must be the global max across ALL agent
    files for this project, not per-agent. seq is project-scoped monotonic."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        resolver = lambda pid: project_root  # noqa: E731

        buf1 = AgentEventBuffer(project_root_resolver=resolver)
        buf1.record("p1", "architect", "text_delta", {"text": "a"})  # seq=1
        buf1.record("p1", "coder", "tool_use_start", {"name": "bash"})  # seq=2
        buf1.record("p1", "architect", "text_delta", {"text": "b"})  # seq=3

        # Fresh buffer hydrates; next record() must produce seq=4, not seq=4
        # accidentally restarted from a per-agent count
        buf2 = AgentEventBuffer(project_root_resolver=resolver)
        buf2.record("p1", "architect", "text_delta", {"text": "c"})

        events = buf2.fetch("p1", agent="architect")
        # Three architect events: seq=1, seq=3, seq=4 (the last one we just added)
        seqs = [e["seq"] for e in events]
        assert seqs == [1, 3, 4]


def test_persistence_hydration_only_runs_once() -> None:
    """Once a project is hydrated, subsequent calls shouldn't re-read disk.
    We can't easily assert filesystem touch counts, but we can verify that
    in-memory state isn't duplicated by repeated fetches."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        resolver = lambda pid: project_root  # noqa: E731

        # Set up some persisted events
        buf1 = AgentEventBuffer(project_root_resolver=resolver)
        for i in range(5):
            buf1.record("p1", "architect", "text_delta", {"text": f"chunk-{i}"})

        buf2 = AgentEventBuffer(project_root_resolver=resolver)
        # Multiple fetches shouldn't keep adding events to the in-memory deque
        for _ in range(3):
            events = buf2.fetch("p1", agent="architect")
            assert len(events) == 5, f"Got {len(events)} events; hydration ran multiple times?"
