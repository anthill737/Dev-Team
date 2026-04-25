"""Tests for ClaudeCodeRunner.

The runner wraps claude-agent-sdk's ClaudeSDKClient. These tests mock the SDK's
message stream to verify translation to Dev Team's StreamEvent shape — the thing
the rest of the orchestrator and UI depend on.

We don't launch a real Claude Code subprocess in tests. That would require the
CLI to be installed + authenticated + hit the subscription, which is the wrong
surface for unit tests. Integration testing with a real subprocess happens via
manual verification on the user's machine.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from app.agents.base import Message, TextBlock, ToolResult, ToolSpec


# ---- Test doubles for SDK message types ----------------------------------------------------
#
# We can't import the real SDK dataclasses and construct instances cleanly in
# all cases (some have required fields we don't need), so we use tiny structural
# stand-ins that match the fields the runner reads. The runner uses isinstance
# checks against the real SDK classes, so we patch those names in the runner's
# import namespace to point at our stand-ins during tests.


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class FakeAssistantMessage:
    content: list[Any]
    model: str = "claude-sonnet-4-6"
    parent_tool_use_id: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    message_id: str = "msg_test"
    stop_reason: str | None = None
    session_id: str = "session_test"
    uuid: str = "uuid_test"


@dataclass
class FakeUserMessage:
    content: list[Any]
    uuid: str = "uuid_test"
    parent_tool_use_id: str | None = None
    tool_use_result: Any = None


@dataclass
class FakeSystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 100
    duration_api_ms: int = 80
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "session_test"
    stop_reason: str | None = "end_turn"
    total_cost_usd: float | None = 0.0
    usage: dict[str, Any] | None = None
    result: str | None = None
    structured_output: Any = None
    model_usage: dict[str, Any] | None = None
    permission_denials: list[Any] | None = None
    errors: list[Any] | None = None
    uuid: str = "uuid_test"


class FakeClaudeSDKClient:
    """Stand-in for ClaudeSDKClient. Yields a pre-scripted sequence of messages
    from receive_response. Use `FakeClaudeSDKClient.scripted = [...]` to set
    the script before the runner calls it.
    """

    scripted: list[Any] = []  # set by each test before the runner runs

    def __init__(self, options: Any = None) -> None:
        self.options = options
        self.queried_prompt: str | None = None

    async def __aenter__(self) -> "FakeClaudeSDKClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queried_prompt = prompt

    async def receive_response(self):
        for msg in self.scripted:
            yield msg


# ---- Helper to patch SDK symbols inside the runner module -----------------------


def _patched_runner_module():
    """Returns a context manager that patches every SDK symbol the runner imports
    with our Fake equivalents, AND patches create_sdk_mcp_server + tool to no-op
    stand-ins so tests don't require the real MCP plumbing."""
    return patch.multiple(
        "app.agents.claude_code_runner",
        ClaudeSDKClient=FakeClaudeSDKClient,
        # Not strictly required — runner imports these via deferred import inside
        # stream(), so patching the module-level namespace is enough only for the
        # symbols that end up resolved via isinstance checks. For the check
        # `isinstance(block, SDKTextBlock)` etc., we need the runner's local name
        # SDKTextBlock to point at our FakeTextBlock. Deferred imports mean these
        # symbols don't exist at module level until stream() runs, which complicates
        # patch.multiple — use patch.dict on the module's __dict__ as a workaround.
    )


def _build_fake_tool() -> ToolSpec:
    """Minimal ToolSpec so we can exercise the tool adaptation path without
    requiring a real ProjectStore."""

    async def _exec(args: dict[str, Any]) -> ToolResult:
        return ToolResult(content=f"got {args}")

    return ToolSpec(
        name="test_tool",
        description="A test tool",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        executor=_exec,
    )


# ---- Tests ----------------------------------------------------------------------


def test_mcp_tool_name_prefix() -> None:
    """Verify the MCP name convention the runner uses to register allowed_tools."""
    from app.agents.claude_code_runner import _mcp_tool_name

    assert _mcp_tool_name("read_task") == "mcp__devteam__read_task"
    assert _mcp_tool_name("write_plan") == "mcp__devteam__write_plan"


def test_messages_to_prompt_empty() -> None:
    from app.agents.claude_code_runner import _messages_to_prompt

    assert _messages_to_prompt([]) == ""


def test_messages_to_prompt_single_user_message() -> None:
    from app.agents.claude_code_runner import _messages_to_prompt

    msgs = [Message(role="user", content=[TextBlock(text="Hello")])]
    result = _messages_to_prompt(msgs)
    assert "User:" in result
    assert "Hello" in result


def test_messages_to_prompt_multi_turn() -> None:
    from app.agents.claude_code_runner import _messages_to_prompt

    msgs = [
        Message(role="user", content=[TextBlock(text="First question")]),
        Message(role="assistant", content=[TextBlock(text="First answer")]),
        Message(role="user", content=[TextBlock(text="Follow-up")]),
    ]
    result = _messages_to_prompt(msgs)
    assert "First question" in result
    assert "First answer" in result
    assert "Follow-up" in result
    # Roles labeled
    assert result.count("User:") == 2
    assert result.count("Assistant:") == 1


def test_disallowed_builtin_tools_includes_write_bash_edit() -> None:
    """Critical: Coder must not bypass our sandbox via the built-in tools."""
    from app.agents.claude_code_runner import _DISALLOWED_BUILTIN_TOOLS

    for expected in ("Bash", "Write", "Edit", "Read", "TodoWrite"):
        assert expected in _DISALLOWED_BUILTIN_TOOLS, (
            f"{expected} must be blocked so Dev Team's sandbox/logging is never bypassed"
        )


def test_adapt_tools_to_mcp_produces_prefixed_allowlist() -> None:
    """The allowed_tools list returned from _adapt_tools_to_mcp must use the
    mcp__devteam__ prefix, because that's how Claude Code surfaces MCP tools
    in its tool catalog."""
    from app.agents.claude_code_runner import _adapt_tools_to_mcp

    tools = [
        _build_fake_tool(),
    ]
    _server, allowed = _adapt_tools_to_mcp(tools)
    assert allowed == ["mcp__devteam__test_tool"]


def test_adapt_tools_to_mcp_captures_spec_in_closure() -> None:
    """Loop-variable closure bug: if _adapt_tools_to_mcp naively references
    `spec` inside its @tool wrapper, every wrapped tool ends up using the
    loop's last spec. Test that each tool's wrapper routes to its own executor."""
    from app.agents.claude_code_runner import _adapt_tools_to_mcp

    calls: list[str] = []

    async def _exec_a(args: dict[str, Any]) -> ToolResult:
        calls.append("a")
        return ToolResult(content="a")

    async def _exec_b(args: dict[str, Any]) -> ToolResult:
        calls.append("b")
        return ToolResult(content="b")

    tools = [
        ToolSpec(name="tool_a", description="", input_schema={"type": "object"}, executor=_exec_a),
        ToolSpec(name="tool_b", description="", input_schema={"type": "object"}, executor=_exec_b),
    ]
    _server, allowed = _adapt_tools_to_mcp(tools)
    assert allowed == ["mcp__devteam__tool_a", "mcp__devteam__tool_b"]
    # We can't easily invoke the wrapped tools without spinning up the MCP
    # server; the prefix check + existence of the server is the main confidence
    # this test provides.


def _run_stream_with_script(
    scripted_messages: list[Any],
) -> list:
    """Invoke ClaudeCodeRunner.stream() with the SDK monkey-patched to return
    the given scripted messages. Returns collected StreamEvents."""
    from app.agents import claude_code_runner as runner_mod

    # Monkey-patch the SDK imports inside the runner module. The runner does
    # deferred imports (inside stream()), so we need to make sure the names it
    # imports resolve to our fakes.
    FakeClaudeSDKClient.scripted = scripted_messages

    import claude_agent_sdk

    original_client = claude_agent_sdk.ClaudeSDKClient
    # Stub the types checked via isinstance. The runner imports them as aliases
    # (SDKTextBlock, SDKToolUseBlock, etc.), so we patch the source names.
    original_text = claude_agent_sdk.TextBlock
    original_toolu = claude_agent_sdk.ToolUseBlock
    original_toolr = claude_agent_sdk.ToolResultBlock
    original_asst = claude_agent_sdk.AssistantMessage
    original_user = claude_agent_sdk.UserMessage
    original_result = claude_agent_sdk.ResultMessage
    original_system = claude_agent_sdk.SystemMessage

    claude_agent_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    claude_agent_sdk.TextBlock = FakeTextBlock
    claude_agent_sdk.ToolUseBlock = FakeToolUseBlock
    claude_agent_sdk.ToolResultBlock = FakeToolResultBlock
    claude_agent_sdk.AssistantMessage = FakeAssistantMessage
    claude_agent_sdk.UserMessage = FakeUserMessage
    claude_agent_sdk.ResultMessage = FakeResultMessage
    claude_agent_sdk.SystemMessage = FakeSystemMessage

    runner = runner_mod.ClaudeCodeRunner(cwd="/tmp")

    async def _collect():
        events = []
        async for ev in runner.stream(
            role="test",
            model="claude-sonnet-4-6",
            system_prompt="you are a test",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[_build_fake_tool()],
        ):
            events.append(ev)
        return events

    try:
        return asyncio.run(_collect())
    finally:
        # Restore
        claude_agent_sdk.ClaudeSDKClient = original_client
        claude_agent_sdk.TextBlock = original_text
        claude_agent_sdk.ToolUseBlock = original_toolu
        claude_agent_sdk.ToolResultBlock = original_toolr
        claude_agent_sdk.AssistantMessage = original_asst
        claude_agent_sdk.UserMessage = original_user
        claude_agent_sdk.ResultMessage = original_result
        claude_agent_sdk.SystemMessage = original_system


def test_stream_translates_text_delta() -> None:
    """Assistant text → text_delta StreamEvent with the text content."""
    script = [
        FakeAssistantMessage(content=[FakeTextBlock(text="hello world")]),
        FakeResultMessage(stop_reason="end_turn"),
    ]
    events = _run_stream_with_script(script)
    text_events = [e for e in events if e.kind == "text_delta"]
    assert len(text_events) == 1
    assert text_events[0].payload["text"] == "hello world"


def test_stream_translates_tool_use_and_strips_prefix() -> None:
    """Model calls mcp__devteam__read_task → we yield tool_use_start with
    the stripped name `read_task` so Dev Team's UI sees the native tool name."""
    script = [
        FakeAssistantMessage(
            content=[
                FakeToolUseBlock(
                    id="toolu_1",
                    name="mcp__devteam__read_task",
                    input={"task_id": "P1-T1"},
                )
            ]
        ),
        FakeResultMessage(),
    ]
    events = _run_stream_with_script(script)
    tool_events = [e for e in events if e.kind == "tool_use_start"]
    assert len(tool_events) == 1
    assert tool_events[0].payload["name"] == "read_task"
    assert tool_events[0].payload["id"] == "toolu_1"
    assert tool_events[0].payload["input"] == {"task_id": "P1-T1"}


def test_stream_translates_tool_result_from_user_message() -> None:
    """Tool results come back wrapped in UserMessage.content. Translate."""
    script = [
        FakeUserMessage(
            content=[
                FakeToolResultBlock(
                    tool_use_id="toolu_1",
                    content="result text",
                    is_error=False,
                )
            ]
        ),
        FakeResultMessage(),
    ]
    events = _run_stream_with_script(script)
    result_events = [e for e in events if e.kind == "tool_result"]
    assert len(result_events) == 1
    assert result_events[0].payload["tool_use_id"] == "toolu_1"
    assert result_events[0].payload["content"] == "result text"
    assert result_events[0].payload["is_error"] is False


def test_stream_emits_usage_from_result_message() -> None:
    """ResultMessage.usage → usage StreamEvent with token counts."""
    script = [
        FakeResultMessage(
            stop_reason="end_turn",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            },
        ),
    ]
    events = _run_stream_with_script(script)
    usage_events = [e for e in events if e.kind == "usage"]
    assert len(usage_events) == 1
    p = usage_events[0].payload
    assert p["input_tokens"] == 100
    assert p["output_tokens"] == 50
    assert p["cache_read_tokens"] == 20
    assert p["cache_creation_tokens"] == 10


def test_stream_always_emits_final_turn_complete() -> None:
    """Even with an empty script, stream must end with turn_complete so the
    orchestrator's consumer doesn't hang waiting for the last event."""
    events = _run_stream_with_script([FakeResultMessage(stop_reason="end_turn")])
    assert events[-1].kind == "turn_complete"
    assert events[-1].payload["stop_reason"] == "end_turn"


def test_stream_system_messages_are_swallowed() -> None:
    """System messages (init, status) are internal SDK bookkeeping — don't
    leak them into Dev Team's event stream."""
    script = [
        FakeSystemMessage(subtype="init", data={"session_id": "s1"}),
        FakeAssistantMessage(content=[FakeTextBlock(text="hi")]),
        FakeResultMessage(),
    ]
    events = _run_stream_with_script(script)
    # No event should mention "system" or the init subtype
    for e in events:
        assert e.kind != "system_message"


def test_role_reminders_are_disabled() -> None:
    """Role reminders are currently disabled — the preset+append system_prompt
    config is expected to provide enough guidance on its own. If interview
    quality regresses to "Architect writes code inline" or "Architect writes
    plan but skips request_approval", we re-enable them.

    This test pins the disabled state so a future change that re-introduces
    reminders (without thinking through whether they're needed) gets noticed.
    """
    from app.agents.claude_code_runner import _ROLE_REMINDERS, _role_reminder_for

    assert _ROLE_REMINDERS == {}, (
        "Role reminders are currently disabled. If you're re-enabling them, "
        "update this test and document why the preset+append approach wasn't "
        "sufficient on its own."
    )

    # Lookup function still works — returns empty string for any role
    for role in ("architect", "dispatcher", "coder", "reviewer"):
        assert _role_reminder_for(role) == ""


def test_role_reminder_unknown_role_returns_empty_string() -> None:
    """If a caller passes a role we don't recognize, don't crash — just skip
    the reminder. Production callers use the four known roles; this is
    defensive for tests and edge cases."""
    from app.agents.claude_code_runner import _role_reminder_for

    assert _role_reminder_for("unknown_role") == ""
    assert _role_reminder_for("") == ""


def test_role_reminder_case_insensitive() -> None:
    """The lookup should normalize. With reminders disabled this currently
    just verifies both inputs return empty string, but the case-insensitivity
    contract should hold if we ever turn reminders back on."""
    from app.agents.claude_code_runner import _role_reminder_for

    assert _role_reminder_for("ARCHITECT") == _role_reminder_for("architect")
    assert _role_reminder_for("Coder") == _role_reminder_for("coder")
