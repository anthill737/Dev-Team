"""ClaudeCodeRunner — subprocess-based AgentRunner using the user's Claude Code subscription.

Unlike APIRunner (which hits /v1/messages with an API key and bills per token), this runner
spawns Claude Code subprocesses via the `claude-agent-sdk` Python package. The subprocess
authenticates from the user's local Claude Code config (set up via `claude setup-token` or
interactive `claude` login), so usage comes out of their Pro/Max subscription rather than
the API account.

Trade-offs vs APIRunner:
  - No per-token billing; uses subscription quota (5-hour rolling window).
  - Slower per-call cold-start (~2-3 seconds to spin up the subprocess).
  - More moving parts: relies on `claude` CLI being installed and authenticated.
  - Same tool semantics — each Dev Team ToolSpec is exposed as an MCP tool via
    `@tool` decorator + `create_sdk_mcp_server`. The model calls them exactly as
    it does through the API.

Architecture:
  - Per-agent-turn, we build a fresh MCP server containing only that agent's tools
    (Architect has architect tools, Coder has coder tools, etc.). Cheap since the
    server is in-process — no subprocess, no network.
  - `disallowed_tools` blocks Claude Code's built-in Bash/Write/Edit/Read/Glob so
    Coder is forced through our custom bash/fs tools (which log to .devteam and use
    our sandbox). Without this, Coder would happily use the built-in tools and
    bypass our observability.
  - `setting_sources=None` disables auto-loading of ~/.claude/skills etc. — we want
    a deterministic, clean agent environment. Use `extra_args={"bare": None}` as the
    CLI equivalent where applicable.

Tool naming:
  When the model calls an MCP tool, Claude Code prefixes the tool name with the
  server name in the form `mcp__<server>__<tool>`. We register the server as
  `devteam`, so our `read_task` tool appears to the model as `mcp__devteam__read_task`.
  The `allowed_tools` list must use these prefixed names. See the `_mcp_tool_name`
  helper below.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from .base import (
    AgentRunResult,
    Message,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

# Name for the in-process MCP server that holds Dev Team's custom tools.
# Shows up in the model's tool names as mcp__devteam__<tool_name>.
_MCP_SERVER_NAME = "devteam"


def _mcp_tool_name(tool_name: str) -> str:
    """Translate a ToolSpec.name to the name Claude Code uses when the model calls it.

    MCP tools are invoked as mcp__<server>__<tool>. The allowed_tools and
    disallowed_tools lists must use this prefixed form.
    """
    return f"mcp__{_MCP_SERVER_NAME}__{tool_name}"


# Claude Code's built-in tools we explicitly disallow — Dev Team's agents must use
# our custom equivalents (which log to .devteam, run through our sandbox, etc.).
# Leaving these allowed would let the Coder bypass our observability layer entirely.
_DISALLOWED_BUILTIN_TOOLS = [
    "Bash",          # Coder must use our bash tool (sandbox + logging)
    "Write",         # Coder writes via bash ("bash" cmd with file redirection)
    "Edit",          # Edits via bash too
    "NotebookEdit",  # Not relevant; block for safety
    "Read",          # Read via our fs_read which is logged
    "Glob",          # Our fs_list equivalent
    "Grep",          # Let Coder use bash + grep instead (logged)
    "WebFetch",      # Not part of Dev Team's tool set
    "WebSearch",     # Not part of Dev Team's tool set
    "TodoWrite",     # We use our own task list (.devteam/tasks.json), not Claude Code's
    "Task",          # No nested subagents — Dev Team's orchestrator is the planner
]


def _adapt_tools_to_mcp(tools: list[ToolSpec]) -> tuple[Any, list[str]]:
    """Wrap each ToolSpec as an MCP @tool and bundle them into an in-process MCP server.

    Returns (mcp_server_config, list_of_mcp_prefixed_tool_names) suitable for
    ClaudeAgentOptions.mcp_servers and .allowed_tools respectively.

    The @tool decorator wants an async function whose return shape is
    `{"content": [{"type": "text", "text": ...}], "is_error": bool (optional)}`.
    Our ToolSpec.executor already returns a ToolResult with `content` and
    `is_error`, so the adapter is mostly translation — plus wrapping string
    content in the MCP content-block shape.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []
    allowed = []

    for spec in tools:
        # Capture spec in the closure. Python late-binds by default; without this
        # `spec_captured = spec` trick every wrapped tool would reference whichever
        # spec was last in the loop.
        spec_captured = spec

        @tool(
            spec_captured.name,
            spec_captured.description,
            spec_captured.input_schema,
        )
        async def _wrapped(args: dict[str, Any], _spec=spec_captured) -> dict[str, Any]:
            """Bridge: SDK calls us with the model's args dict; we call the ToolSpec's
            executor and translate its ToolResult back to MCP content-block format."""
            try:
                result = await _spec.executor(args)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Tool %s raised an unhandled exception", _spec.name
                )
                return {
                    "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                    "is_error": True,
                }
            # ToolResult.content can be str or list[dict]. MCP wants list[dict] in
            # content-block format. Normalize:
            if isinstance(result.content, str):
                content_blocks = [{"type": "text", "text": result.content}]
            else:
                content_blocks = result.content
            out: dict[str, Any] = {"content": content_blocks}
            if result.is_error:
                out["is_error"] = True
            return out

        sdk_tools.append(_wrapped)
        allowed.append(_mcp_tool_name(spec_captured.name))

    server = create_sdk_mcp_server(
        name=_MCP_SERVER_NAME,
        version="1.0.0",
        tools=sdk_tools,
    )
    return server, allowed


# Role reminder strings prepended to the user prompt. Blunt, short, imperative.
# Role reminders — currently disabled.
#
# In an earlier iteration we prepended a blunt role-reinforcement preamble to
# every user prompt (e.g., "[ROLE LOCK — ARCHITECT MODE] ..."). This was added
# to fix a failure where the Architect wrote code inline instead of conducting
# an interview. It worked but had a cost: interview quality measurably degraded
# vs. the API runner. The Architect started producing rigid, scripted-feeling
# responses, repeating questions the user had already answered, and reading
# more like a checklist executor than a senior engineer.
#
# Hypothesis: the system_prompt change (preset+append form, which gives us
# Claude Code's full tool-aware system prompt + our Dev Team directives) is
# enough on its own. The role reminder was a bandage on a symptom that the
# preset change actually fixed. By keeping both, we were drowning the
# Architect's full ~1400-token nuanced instructions under a recent ~100-token
# blunt directive that dominated attention.
#
# This change strips the reminders. If interview quality returns to v1 (API
# runner) levels, we're done. If the Architect regresses to writing code
# inline, the reminder was load-bearing and we need a smarter fix — probably
# a more nuanced reinforcement injected into the system prompt itself rather
# than the user message.
_ROLE_REMINDERS: dict[str, str] = {}


def _role_reminder_for(role: str) -> str:
    """Return a role-reinforcement preamble for the user prompt.

    Currently always empty — see _ROLE_REMINDERS doc above for context. Kept
    as a function (rather than inlined) so it can be re-enabled without
    touching the call site if the experiment fails.
    """
    return _ROLE_REMINDERS.get(role.lower(), "")


def _messages_to_prompt(messages: list[Message]) -> str:
    """Flatten Dev Team's message list into the single string prompt Claude Code wants.

    The CLI's `-p` mode takes one prompt string. When we have a multi-turn conversation
    (e.g., Architect with prior interview turns), we serialize them into a readable
    transcript prefix that the model sees as context, followed by the latest user message.

    This is lossy vs. the API's structured message list, but the model handles it fine —
    and the system prompt, which is passed separately, does the heavy lifting.
    """
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        role_label = "User" if m.role == "user" else "Assistant"
        text_fragments: list[str] = []
        for block in m.content:
            if isinstance(block, TextBlock):
                text_fragments.append(block.text)
            elif isinstance(block, ToolUseBlock):
                # Prior tool calls stay in context as readable log entries; the model
                # doesn't need to "re-call" them, just know they happened.
                text_fragments.append(
                    f"[tool call: {block.name}({block.input!r})]"
                )
            elif isinstance(block, ToolResultBlock):
                content = block.content
                text_fragments.append(f"[tool result: {content!r}]")
        if text_fragments:
            parts.append(f"{role_label}: {' '.join(text_fragments)}")
    return "\n\n".join(parts)


class ClaudeCodeRunner:
    """AgentRunner implementation that drives Claude Code via claude-agent-sdk.

    Uses the user's subscription for billing (no API key). Same Protocol as
    APIRunner, so it's a drop-in swap at the orchestrator level.
    """

    def __init__(self, *, cwd: str | None = None) -> None:
        """
        cwd: working directory Claude Code should start in. Usually the project
        root. Defaults to the current process cwd, which is rarely right —
        callers should pass the ProjectStore.root explicitly.
        """
        self._cwd = cwd

    # --- Public API -----------------------------------------------------------------------------

    async def run(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int = 4096,
        max_iterations: int = 20,
        wall_clock_seconds: float | None = None,
    ) -> AgentRunResult:
        """Run to completion, collecting all stream events into a final result."""
        final_text_parts: list[str] = []
        total_in = 0
        total_out = 0
        total_cache_read = 0
        total_cache_creation = 0
        tool_calls = 0
        stop_reason: str | None = None
        message_log = list(messages)

        async for ev in self.stream(
            role=role,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            wall_clock_seconds=wall_clock_seconds,
        ):
            if ev.kind == "text_delta":
                final_text_parts.append(ev.payload.get("text", ""))
            elif ev.kind == "tool_use_start":
                tool_calls += 1
            elif ev.kind == "usage":
                total_in += int(ev.payload.get("input_tokens", 0))
                total_out += int(ev.payload.get("output_tokens", 0))
                total_cache_read += int(ev.payload.get("cache_read_tokens", 0))
                total_cache_creation += int(
                    ev.payload.get("cache_creation_tokens", 0)
                )
            elif ev.kind == "turn_complete":
                stop_reason = ev.payload.get("stop_reason")

        return AgentRunResult(
            final_text="".join(final_text_parts),
            messages=message_log,
            tokens_input=total_in,
            tokens_output=total_out,
            cache_read_tokens=total_cache_read,
            cache_creation_tokens=total_cache_creation,
            stop_reason=stop_reason,
            tool_calls_made=tool_calls,
        )

    async def stream(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int = 4096,  # noqa: ARG002 — unused; kept for Protocol compat
        max_iterations: int = 20,
        wall_clock_seconds: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run with streaming events. Yields text/tool/usage events matching APIRunner's shape."""
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
            TextBlock as SDKTextBlock,
            ThinkingBlock as SDKThinkingBlock,
            ToolResultBlock as SDKToolResultBlock,
            ToolUseBlock as SDKToolUseBlock,
            UserMessage,
        )

        mcp_server, allowed_tools = _adapt_tools_to_mcp(tools)

        # Build the single prompt string: any prior conversation as prefix,
        # plus the final user turn. Claude Code doesn't have a native multi-turn
        # API like the Messages API; -p mode takes one prompt. We serialize
        # context so the model sees it.
        #
        # ROLE REINFORCEMENT — empirically necessary. When we ship the Architect
        # prompt via system_prompt, the SDK doesn't always make the model adopt
        # its role convincingly (observed: model writes code in response to
        # "hello world Python script" instead of conducting an interview).
        # Restating the role + tool obligations at the START of the user message
        # ensures the model can't just ignore them. Uses a short, blunt preamble;
        # the meaningful content still comes from system_prompt.
        role_reminder = _role_reminder_for(role)
        user_prompt = _messages_to_prompt(messages)
        prompt = f"{role_reminder}\n\n{user_prompt}" if role_reminder else user_prompt

        # SYSTEM PROMPT STRATEGY — use the preset+append form, not plain string.
        #
        # Why: the SDK's default (plain-string mode or no system_prompt) uses a
        # "minimal system prompt" per Anthropic docs — missing the tool-calling
        # scaffolding and behavioral priming that make Claude Code agents
        # actually invoke tools. In plain-string mode, the model often reverts
        # to generic Claude-style conversational responses (writes code inline
        # instead of calling write_plan, ignores role directives).
        #
        # preset=claude_code + append=our_prompt gives us:
        #   - Claude Code's full tool-aware system prompt (instruction-following
        #     scaffolding, tool-call conventions)
        #   - Our Dev Team role-specific directives appended on top
        # This is the pattern Anthropic recommends in the Modifying System
        # Prompts docs for SDK users who need custom instructions AND reliable
        # tool use.
        system_prompt_config: Any = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        }

        options = ClaudeAgentOptions(
            system_prompt=system_prompt_config,
            allowed_tools=allowed_tools,
            disallowed_tools=_DISALLOWED_BUILTIN_TOOLS,
            mcp_servers={_MCP_SERVER_NAME: mcp_server},
            model=model,
            max_turns=max_iterations,
            cwd=self._cwd,
            # acceptEdits: approve file edits without prompting. Since our tools
            # do their own permission model (sandbox, budget), we don't want
            # Claude Code popping interactive prompts. Our disallowed list blocks
            # the built-in file-mutating tools anyway; this is belt + suspenders.
            permission_mode="acceptEdits",
            # Don't auto-load user's personal ~/.claude settings, project CLAUDE.md,
            # hooks, skills. Dev Team's runs should be deterministic and not depend
            # on what's sitting in the user's filesystem config.
            setting_sources=None,
        )

        logger.info(
            "ClaudeCodeRunner invoking role=%s model=%s allowed_tools=%s "
            "system_prompt_kind=preset+append append_chars=%d prompt_chars=%d",
            role,
            model,
            allowed_tools,
            len(system_prompt),
            len(prompt),
        )

        start = time.monotonic()
        stop_reason: str | None = None

        async def _inner() -> AsyncIterator[StreamEvent]:
            nonlocal stop_reason
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    # SystemMessage: init, status, etc. — log but don't forward.
                    if isinstance(msg, SystemMessage):
                        logger.debug(
                            "Claude Code system message: %s", msg.subtype
                        )
                        continue

                    # AssistantMessage: the meat. Contains text, thinking,
                    # tool_use blocks. Each block type maps to its own
                    # StreamEvent kind so the inspector can render them
                    # distinctly (thinking → italic dim, text → normal,
                    # tool_use → tool card).
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, SDKThinkingBlock):
                                # Thinking is the model's private reasoning
                                # before the user-facing response. Surfacing
                                # it makes the agent's behavior legible: you
                                # see WHY the Coder decided to read main.ts,
                                # not just THAT it did.
                                yield StreamEvent(
                                    kind="thinking_delta",
                                    payload={"text": block.thinking},
                                )
                            elif isinstance(block, SDKTextBlock):
                                yield StreamEvent(
                                    kind="text_delta",
                                    payload={"text": block.text},
                                )
                            elif isinstance(block, SDKToolUseBlock):
                                # Strip the mcp__devteam__ prefix when surfacing
                                # to Dev Team's UI — our decisions log, live
                                # execution view, etc. expect the bare tool name.
                                display_name = block.name
                                if display_name.startswith(
                                    f"mcp__{_MCP_SERVER_NAME}__"
                                ):
                                    display_name = display_name.split("__", 2)[2]
                                yield StreamEvent(
                                    kind="tool_use_start",
                                    payload={
                                        "id": block.id,
                                        "name": display_name,
                                        "input": block.input,
                                    },
                                )
                        # Per-turn usage tallies — the SDK emits usage on
                        # AssistantMessage.usage (a dict) when available.
                        if msg.usage:
                            yield StreamEvent(
                                kind="usage",
                                payload={
                                    "input_tokens": msg.usage.get(
                                        "input_tokens", 0
                                    ),
                                    "output_tokens": msg.usage.get(
                                        "output_tokens", 0
                                    ),
                                    "cache_read_tokens": msg.usage.get(
                                        "cache_read_input_tokens", 0
                                    ),
                                    "cache_creation_tokens": msg.usage.get(
                                        "cache_creation_input_tokens", 0
                                    ),
                                },
                            )

                    # UserMessage: tool_result blocks coming back from the MCP server.
                    # Useful for UI live-feed; our ToolSpec executor already handled
                    # the side effect, but the UI likes to show the tool result.
                    elif isinstance(msg, UserMessage):
                        for block in msg.content:
                            if isinstance(block, SDKToolResultBlock):
                                yield StreamEvent(
                                    kind="tool_result",
                                    payload={
                                        "tool_use_id": block.tool_use_id,
                                        "content": block.content,
                                        "is_error": bool(block.is_error),
                                    },
                                )

                    # ResultMessage: final envelope with totals. Fires once at end.
                    elif isinstance(msg, ResultMessage):
                        stop_reason = msg.stop_reason or "end_turn"
                        # Emit a final usage event with totals if the SDK gave us
                        # model_usage (more comprehensive than per-turn usage).
                        if msg.usage:
                            yield StreamEvent(
                                kind="usage",
                                payload={
                                    "input_tokens": msg.usage.get(
                                        "input_tokens", 0
                                    ),
                                    "output_tokens": msg.usage.get(
                                        "output_tokens", 0
                                    ),
                                    "cache_read_tokens": msg.usage.get(
                                        "cache_read_input_tokens", 0
                                    ),
                                    "cache_creation_tokens": msg.usage.get(
                                        "cache_creation_input_tokens", 0
                                    ),
                                },
                            )

        # Wrap the whole stream in wall_clock_seconds timeout if requested.
        # Do it carefully — we need to preserve the async iterator semantics,
        # not just await a single coroutine.
        try:
            if wall_clock_seconds is not None:
                # asyncio.wait_for doesn't work on async generators directly.
                # Instead, check elapsed time on each yield and bail if exceeded.
                async for ev in _inner():
                    if time.monotonic() - start > wall_clock_seconds:
                        stop_reason = "wall_clock_timeout"
                        break
                    yield ev
            else:
                async for ev in _inner():
                    yield ev
        except Exception as exc:  # noqa: BLE001
            # Surface error as a stream event so the caller sees it without the
            # generator just dying silently. The exception type tells us
            # whether this is a subprocess / auth / rate-limit issue.
            kind = "error"
            message = str(exc)
            try:
                from claude_agent_sdk import (
                    CLIConnectionError,
                    CLINotFoundError,
                    ProcessError,
                )
                if isinstance(exc, CLINotFoundError):
                    message = (
                        "Claude Code CLI not found. Install it: "
                        "curl -fsSL https://claude.ai/install.sh | bash"
                    )
                elif isinstance(exc, CLIConnectionError):
                    message = (
                        "Failed to connect to Claude Code. Make sure you're "
                        "logged in: run `claude` and follow the prompts, or "
                        "`claude setup-token` for an OAuth token."
                    )
                elif isinstance(exc, ProcessError):
                    message = f"Claude Code subprocess failed: {exc}"
            except ImportError:
                pass
            logger.exception("ClaudeCodeRunner stream error")
            yield StreamEvent(kind=kind, payload={"message": message})
            stop_reason = "error"

        yield StreamEvent(
            kind="turn_complete",
            payload={"stop_reason": stop_reason or "end_turn"},
        )
