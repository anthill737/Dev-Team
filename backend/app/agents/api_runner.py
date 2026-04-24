"""APIRunner — implements AgentRunner using direct Anthropic API calls.

This is the v1 backend. A future ClaudeCodeRunner will implement the same interface by driving
a Claude Code session instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from .base import (
    AgentRunResult,
    ContentBlock,
    Message,
    StreamEvent,
    TextBlock,
    ToolResult,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


class APIRunner:
    """Direct Anthropic API implementation of the AgentRunner protocol."""

    def __init__(self, api_key: str, *, default_max_tokens: int = 4096) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._default_max_tokens = default_max_tokens

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
        """Run to completion without streaming. Handles the full tool-use loop.

        wall_clock_seconds caps total wall time if provided. Useful for agents
        like the Dispatcher that don't have a natural iteration cap — lets them
        think as long as they need but prevents a true runaway loop.
        """
        import time as _time

        working_messages = [_to_api_message(m) for m in messages]
        tool_defs = [_to_api_tool(t) for t in tools] if tools else None
        tool_executors = {t.name: t.executor for t in tools}

        total_in = 0
        total_out = 0
        tool_calls = 0
        stop_reason: str | None = None
        final_text_parts: list[str] = []
        start_time = _time.monotonic()
        full_message_log: list[Message] = list(messages)

        for iteration in range(max_iterations):
            if (
                wall_clock_seconds is not None
                and _time.monotonic() - start_time > wall_clock_seconds
            ):
                stop_reason = "wall_clock_timeout"
                break
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=working_messages,
                tools=tool_defs if tool_defs else [],
            )

            total_in += response.usage.input_tokens
            total_out += response.usage.output_tokens
            stop_reason = response.stop_reason

            # Rebuild assistant message from response blocks
            assistant_blocks: list[ContentBlock] = []
            api_assistant_blocks: list[dict[str, Any]] = []

            for block in response.content:
                if block.type == "text":
                    assistant_blocks.append(TextBlock(text=block.text))
                    api_assistant_blocks.append({"type": "text", "text": block.text})
                    final_text_parts.append(block.text)
                elif block.type == "tool_use":
                    assistant_blocks.append(
                        ToolUseBlock(id=block.id, name=block.name, input=dict(block.input))
                    )
                    api_assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            full_message_log.append(Message(role="assistant", content=assistant_blocks))
            working_messages.append({"role": "assistant", "content": api_assistant_blocks})

            if response.stop_reason != "tool_use":
                break

            # Execute all tool calls from this turn, collecting results for the next user message
            tool_results_for_next: list[dict[str, Any]] = []
            tool_result_blocks: list[ContentBlock] = []

            for block in assistant_blocks:
                if isinstance(block, ToolUseBlock):
                    tool_calls += 1
                    executor = tool_executors.get(block.name)
                    if executor is None:
                        result = ToolResult(
                            content=f"Unknown tool: {block.name}", is_error=True
                        )
                    else:
                        try:
                            result = await executor(block.input)
                        except Exception as exc:  # noqa: BLE001 — we want to surface any tool error
                            logger.exception("Tool %s raised for agent %s", block.name, role)
                            result = ToolResult(
                                content=f"Tool raised exception: {type(exc).__name__}: {exc}",
                                is_error=True,
                            )

                    tool_results_for_next.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
                    tool_result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.id,
                            content=result.content,
                            is_error=result.is_error,
                        )
                    )

            full_message_log.append(Message(role="user", content=tool_result_blocks))
            working_messages.append({"role": "user", "content": tool_results_for_next})
        else:
            logger.warning(
                "Agent %s hit max_iterations=%d without natural stop", role, max_iterations
            )
            stop_reason = stop_reason or "max_iterations"

        return AgentRunResult(
            final_text="\n".join(p for p in final_text_parts if p).strip(),
            messages=full_message_log,
            tokens_input=total_in,
            tokens_output=total_out,
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
        max_tokens: int = 4096,
        max_iterations: int = 20,
        wall_clock_seconds: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run with streaming events. Yields live deltas and a final turn_complete event.

        wall_clock_seconds caps total wall time if provided. See run() for rationale.
        """
        import time as _time

        working_messages = [_to_api_message(m) for m in messages]
        tool_defs = [_to_api_tool(t) for t in tools] if tools else None
        tool_executors = {t.name: t.executor for t in tools}

        total_in = 0
        total_out = 0
        total_cache_read = 0
        total_cache_creation = 0
        tool_calls = 0
        stop_reason: str | None = None
        final_text_parts: list[str] = []
        full_message_log: list[Message] = list(messages)
        start_time = _time.monotonic()

        for _iteration in range(max_iterations):
            if (
                wall_clock_seconds is not None
                and _time.monotonic() - start_time > wall_clock_seconds
            ):
                stop_reason = "wall_clock_timeout"
                break
            assistant_blocks: list[ContentBlock] = []
            api_assistant_blocks: list[dict[str, Any]] = []

            # Prompt caching: the system prompt and tool definitions are identical
            # on every turn of a given agent run. Marking them cacheable gets us a
            # 90% discount on input tokens for cache hits. Cache lives 5 min.
            # Format: system becomes a list of blocks, last one has cache_control.
            # Tools: last tool in the array has cache_control (caches all tools up
            # to and including that breakpoint).
            cached_system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            cached_tools = list(tool_defs) if tool_defs else []
            if cached_tools:
                cached_tools = [
                    {**t, **({"cache_control": {"type": "ephemeral"}} if i == len(cached_tools) - 1 else {})}
                    for i, t in enumerate(cached_tools)
                ]

            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=cached_system,
                messages=working_messages,
                tools=cached_tools,
            ) as stream:
                async for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", "") == "text_delta":
                            yield StreamEvent(
                                kind="text_delta", payload={"text": delta.text}
                            )

                final_message = await stream.get_final_message()

            total_in += final_message.usage.input_tokens
            total_out += final_message.usage.output_tokens
            stop_reason = final_message.stop_reason

            # Cache stats — Anthropic reports these on usage. cache_read = hits
            # (90% off), cache_creation = misses that wrote to cache. If these are
            # zero across a multi-turn run, caching isn't working.
            cache_read = getattr(final_message.usage, "cache_read_input_tokens", 0) or 0
            cache_creation = getattr(final_message.usage, "cache_creation_input_tokens", 0) or 0
            total_cache_read += cache_read
            total_cache_creation += cache_creation

            yield StreamEvent(
                kind="usage",
                payload={
                    "input_tokens": final_message.usage.input_tokens,
                    "output_tokens": final_message.usage.output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                },
            )

            for block in final_message.content:
                if block.type == "text":
                    assistant_blocks.append(TextBlock(text=block.text))
                    api_assistant_blocks.append({"type": "text", "text": block.text})
                    final_text_parts.append(block.text)
                elif block.type == "tool_use":
                    assistant_blocks.append(
                        ToolUseBlock(id=block.id, name=block.name, input=dict(block.input))
                    )
                    api_assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                    yield StreamEvent(
                        kind="tool_use_start",
                        payload={"name": block.name, "input": dict(block.input)},
                    )

            full_message_log.append(Message(role="assistant", content=assistant_blocks))
            working_messages.append({"role": "assistant", "content": api_assistant_blocks})

            if final_message.stop_reason != "tool_use":
                break

            tool_results_for_next: list[dict[str, Any]] = []
            tool_result_blocks: list[ContentBlock] = []

            for block in assistant_blocks:
                if isinstance(block, ToolUseBlock):
                    tool_calls += 1
                    executor = tool_executors.get(block.name)
                    if executor is None:
                        result = ToolResult(content=f"Unknown tool: {block.name}", is_error=True)
                    else:
                        try:
                            result = await executor(block.input)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("Tool %s raised for agent %s", block.name, role)
                            result = ToolResult(
                                content=f"Tool raised exception: {type(exc).__name__}: {exc}",
                                is_error=True,
                            )

                    yield StreamEvent(
                        kind="tool_result",
                        payload={
                            "name": block.name,
                            "is_error": result.is_error,
                            # Truncate content for UI; full content is in the message log.
                            "content_preview": _preview(result.content),
                        },
                    )

                    tool_results_for_next.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
                    tool_result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.id,
                            content=result.content,
                            is_error=result.is_error,
                        )
                    )

            full_message_log.append(Message(role="user", content=tool_result_blocks))
            working_messages.append({"role": "user", "content": tool_results_for_next})
        else:
            stop_reason = stop_reason or "max_iterations"

        yield StreamEvent(
            kind="turn_complete",
            payload={
                "result": AgentRunResult(
                    final_text="\n".join(p for p in final_text_parts if p).strip(),
                    messages=full_message_log,
                    tokens_input=total_in,
                    tokens_output=total_out,
                    cache_read_tokens=total_cache_read,
                    cache_creation_tokens=total_cache_creation,
                    stop_reason=stop_reason,
                    tool_calls_made=tool_calls,
                )
            },
        )


# ----- Helpers ----------------------------------------------------------------------------------


def _to_api_message(m: Message) -> MessageParam:
    """Convert an internal Message to the Anthropic API's expected dict shape."""
    api_content: list[dict[str, Any]] = []
    for block in m.content:
        if isinstance(block, TextBlock):
            api_content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            api_content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            api_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
    return {"role": m.role, "content": api_content}  # type: ignore[return-value]


def _to_api_tool(t: ToolSpec) -> dict[str, Any]:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_schema,
    }


def _preview(content: str | list[dict[str, Any]], limit: int = 500) -> str:
    if isinstance(content, str):
        return content[:limit] + ("…" if len(content) > limit else "")
    try:
        import json as _json

        s = _json.dumps(content)[:limit]
        return s + ("…" if len(s) == limit else "")
    except Exception:  # noqa: BLE001
        return str(content)[:limit]
