"""The AgentRunner interface — the abstraction that lets us swap execution backends.

v1 implements this via `APIRunner` (direct Anthropic API calls). v1.5 will add
`ClaudeCodeRunner`, which drives a Claude Code session. The orchestrator depends
only on this interface, not on any specific runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

# ----- Message and content types -----------------------------------------------------------------

Role = Literal["user", "assistant"]


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str | list[dict[str, Any]]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: Role
    content: list[ContentBlock]


# ----- Tools -------------------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Declaration of a tool the agent may call.

    `input_schema` is a JSON Schema describing the tool's arguments. `executor` is an async
    callable that actually performs the tool; the runner invokes it when the model calls the tool.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    executor: Any  # Callable[[dict[str, Any]], Awaitable[ToolResult]] — kept loose to avoid cycles


@dataclass
class ToolResult:
    content: str | list[dict[str, Any]]
    is_error: bool = False


# ----- Run results and streaming events ----------------------------------------------------------


@dataclass
class AgentRunResult:
    """The final result of an agent run, after all tool-use loops have completed."""

    final_text: str
    messages: list[Message]  # full conversation including tool calls
    tokens_input: int = 0
    tokens_output: int = 0
    # Cache breakdown — subset of tokens_input. cache_read is the portion served
    # from cache (10% of input price), cache_creation the portion written to cache
    # (125% of input price). Summing read + creation + uncached input = tokens_input.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stop_reason: str | None = None
    tool_calls_made: int = 0


@dataclass
class StreamEvent:
    """Event emitted during a streaming operation for live UI updates.

    `kind` is a free-form string so different subsystems (agent runner, execution loop,
    task runner) can emit their own event vocabularies without depending on a central
    enum. Known kinds include:

      AgentRunner kinds (from api_runner.py):
        text_delta, tool_use_start, tool_use_input_delta, tool_result,
        turn_complete, usage, error

      ExecutionLoop kinds (from orchestrator/execution_loop.py):
        loop_paused, loop_exit, scheduler_decision, project_complete, phase_complete,
        task_escalated, deadlock, budget_exceeded, task_start, task_blocked,
        loop_safety_halt

      TaskRunner kinds (Coder-specific, added when Coder ships): task_outcome, etc.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


# ----- The interface -----------------------------------------------------------------------------


class AgentRunner(Protocol):
    """An AgentRunner executes an agent's turn — potentially multi-step with tool use.

    Implementations must handle the full tool-use loop internally: when the model requests a
    tool, the runner invokes the tool's executor, feeds the result back to the model, and
    continues until the model produces a final text response or a stopping condition is hit.
    """

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
        """Run the agent to completion, returning the final result."""
        ...

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
        """Run the agent, yielding StreamEvents for live UI updates.

        The final event is always `turn_complete` with the AgentRunResult in its payload.
        """
        ...
