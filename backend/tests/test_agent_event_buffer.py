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
