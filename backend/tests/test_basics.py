"""Core tests: project store behavior, Unicode safety, prompt assembly wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.prompts import (
    REFLECTIVE_PRACTICE_BLOCK,
    architect_prompt,
    coder_prompt,
    dispatcher_prompt,
    reviewer_prompt,
)
from app.state import ProjectStatus, ProjectStore


def test_reflective_practice_block_wired_into_reasoning_role_prompts() -> None:
    """Architect, Coder, and Reviewer all do open-ended reasoning work and benefit from
    the reflective practice protocol. Guards against adding a new reasoning role and
    forgetting to append the block. Deliberately excludes the Dispatcher — see the
    separate test below.
    """
    for role, prompt in [
        ("architect", architect_prompt()),
        ("coder", coder_prompt()),
        ("reviewer", reviewer_prompt()),
    ]:
        assert REFLECTIVE_PRACTICE_BLOCK in prompt, (
            f"{role} prompt is missing the reflective practice block — did you forget "
            f"to call _assemble()?"
        )


def test_dispatcher_prompt_deliberately_excludes_reflective_block() -> None:
    """The Dispatcher's job is a structured commit via write_tasks + mark_dispatch_complete,
    not open-ended reasoning. In practice, including the reflective block caused Sonnet to
    generate minutes of prose before attempting any tool call, hitting max_tokens truncation.
    This test pins the deliberate exclusion so someone doesn't "helpfully" add it back.
    """
    prompt = dispatcher_prompt()
    assert REFLECTIVE_PRACTICE_BLOCK not in prompt, (
        "Dispatcher prompt must NOT include the reflective practice block. The block "
        "prompts verbose reasoning output, which for the Dispatcher's pure tool-call "
        "workflow causes max_tokens truncation before any tool is invoked. If you need "
        "the Dispatcher to think more carefully, add specific targeted guidance to "
        "_DISPATCHER_BODY instead."
    )


def test_project_store_init_creates_devteam_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        meta = store.init(project_id="proj_test", name="Test Project")
        assert meta.id == "proj_test"
        assert meta.name == "Test Project"
        assert meta.status == ProjectStatus.INIT
        assert (Path(tmp) / ".devteam").is_dir()
        assert (Path(tmp) / ".devteam" / "meta.json").is_file()
        assert (Path(tmp) / ".devteam" / "inboxes" / "architect.json").is_file()


def test_project_store_add_token_usage_buckets_by_model() -> None:
    """Token usage by model family must bucket correctly — Opus→opus fields,
    Sonnet→sonnet fields, etc. The aggregate tokens_used always accumulates."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")

        store.add_token_usage("claude-opus-4-7", 1000, 500)
        store.add_token_usage("claude-sonnet-4-6", 2000, 800)
        store.add_token_usage("claude-haiku-4-5-20251001", 300, 100)

        meta = store.read_meta()
        assert meta.tokens_input_opus == 1000
        assert meta.tokens_output_opus == 500
        assert meta.tokens_input_sonnet == 2000
        assert meta.tokens_output_sonnet == 800
        assert meta.tokens_input_haiku == 300
        assert meta.tokens_output_haiku == 100
        # Aggregate includes everything
        assert meta.tokens_used == 1000 + 500 + 2000 + 800 + 300 + 100

        # Subsequent calls accumulate, not replace
        store.add_token_usage("claude-opus-4-7", 100, 50)
        meta = store.read_meta()
        assert meta.tokens_input_opus == 1100
        assert meta.tokens_output_opus == 550


def test_project_store_add_token_usage_ignores_unknown_model() -> None:
    """Unknown model names fall through to the aggregate only — don't accidentally
    credit cost to the wrong bucket if someone adds a new model mid-upgrade."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")
        store.add_token_usage("some-future-model", 1000, 500)
        meta = store.read_meta()
        assert meta.tokens_used == 1500
        assert meta.tokens_input_opus == 0
        assert meta.tokens_input_sonnet == 0
        assert meta.tokens_input_haiku == 0


def test_project_store_add_token_usage_tracks_cache_tokens() -> None:
    """Cache reads and writes must bucket with their model family so the cost
    estimator can apply the right rate (reads at 10%, writes at 125%)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")

        # Sonnet turn with a mix: 5000 input (of which 3000 cache reads, 500 cache writes),
        # 800 output
        store.add_token_usage(
            "claude-sonnet-4-6",
            tokens_input=5000,
            tokens_output=800,
            cache_read=3000,
            cache_creation=500,
        )
        meta = store.read_meta()
        assert meta.tokens_input_sonnet == 5000
        assert meta.tokens_output_sonnet == 800
        assert meta.cache_read_sonnet == 3000
        assert meta.cache_creation_sonnet == 500

        # Accumulate, not replace
        store.add_token_usage(
            "claude-sonnet-4-6",
            tokens_input=2000,
            tokens_output=300,
            cache_read=1500,
            cache_creation=0,
        )
        meta = store.read_meta()
        assert meta.cache_read_sonnet == 4500
        assert meta.cache_creation_sonnet == 500

        # Different model family doesn't leak into sonnet buckets
        store.add_token_usage(
            "claude-opus-4-7",
            tokens_input=100,
            tokens_output=50,
            cache_read=80,
            cache_creation=0,
        )
        meta = store.read_meta()
        assert meta.cache_read_opus == 80
        assert meta.cache_read_sonnet == 4500  # unchanged


def test_project_store_decisions_log_is_append_only() -> None:
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")
        asyncio.run(store.append_decision({"actor": "architect", "kind": "note", "note": "a"}))
        asyncio.run(store.append_decision({"actor": "architect", "kind": "note", "note": "b"}))
        entries = store.read_decisions()
        assert len(entries) == 2
        assert entries[0]["note"] == "a"
        assert entries[1]["note"] == "b"
        for e in entries:
            assert "timestamp" in e


def test_project_store_handles_unicode_in_plan() -> None:
    """Regression: Windows default codec (cp1252) choked on em-dash and smart quotes.
    This is the exact failure mode that killed the first Pong plan write."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")
        plan = (
            "# Brainrot Pong — Plan\n\n"
            "## Summary\n"
            "A browser-based Pong clone where every paddle hit spawns “brainrot” slang.\n"
            "Emoji smoke test: 🎮 ✨ 💀\n"
            "Arrows: → ← ↑ ↓\n"
        )
        store.write_plan(plan)
        assert store.read_plan() == plan


def test_project_store_handles_unicode_in_decisions() -> None:
    """Same regression for the decisions log, which is the highest-volume write path."""
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")
        asyncio.run(
            store.append_decision({
                "actor": "architect",
                "kind": "note",
                "note": "Em-dash — and curly quotes “like this” and emoji 🚀",
            })
        )
        entries = store.read_decisions()
        assert len(entries) == 1
        assert "🚀" in entries[0]["note"]
        assert "—" in entries[0]["note"]


def test_project_store_handles_unicode_in_interview() -> None:
    """And the interview log, which captures raw user prose."""
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")
        asyncio.run(store.append_interview("user", "I want — specifically — a 🎮 game"))
        log = store.read_interview()
        assert len(log) == 1
        assert log[0]["content"] == "I want — specifically — a 🎮 game"


def test_project_store_inbox_messages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(tmp)
        store.init(project_id="proj_test", name="Test Project")

        msg = store.append_inbox(
            to_role="coder",
            from_role="dispatcher",
            subject="Task P1-T1 assigned",
            body="Please implement signup",
        )
        assert msg.id
        unread = store.read_inbox("coder", unread_only=True)
        assert len(unread) == 1
        store.mark_inbox_read("coder", [msg.id])
        unread_after = store.read_inbox("coder", unread_only=True)
        assert len(unread_after) == 0
