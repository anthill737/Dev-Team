# Dev Team Architecture

## Design Principles

1. **Shared artifacts first, messages second.** The state of the project lives in files (`plan.md`, `tasks.json`, `decisions.log`). Agents are mostly stateless between turns and read from disk. This makes the system debuggable — you can always open the files and see what the system "thinks."
2. **External verification over introspection.** Agents run tests, type checkers, and builds. Self-critique is a complement, not a substitute.
3. **Reflective practice is mandatory.** Every agent enters reflective practice at work boundaries, asking both *completeness* (did I address every requirement?) and *viability* (would this hold up in production?) questions as distinct cognitive modes. See `docs/PROMPTS.md`.
4. **Swappable execution backend.** The `AgentRunner` interface abstracts how an agent actually runs. v1 uses direct Anthropic API calls (`APIRunner`). v1.5 will add `ClaudeCodeRunner` for Max subscription users. The orchestrator doesn't know or care which is used.
5. **Tools are scoped per role.** Architect gets web search and chat. Coder gets file I/O, bash, test execution. Reviewer gets file read and test execution. Limiting tools per role reduces context, improves focus, and acts as a safety layer.

## Layers

```
┌─────────────────────────────────────────────────┐
│ Frontend (React + Monaco)                       │
│  • Architect chat  • Plan  • Tasks              │
│  • File tree / editor  • Activity log  • Inbox  │
└──────────────────┬──────────────────────────────┘
                   │  HTTP + WebSocket
┌──────────────────▼──────────────────────────────┐
│ Backend (FastAPI)                               │
│  ┌───────────────────────────────────────────┐  │
│  │ Orchestrator (state machine)              │  │
│  │   INTERVIEW → PLANNING → AWAIT_APPROVAL   │  │
│  │   → EXECUTING → PHASE_REVIEW → COMPLETE   │  │
│  └────────────┬──────────────────────────────┘  │
│  ┌────────────▼──────────────────────────────┐  │
│  │ Agents: Architect, Dispatcher,            │  │
│  │         Coder, Reviewer                   │  │
│  └────────────┬──────────────────────────────┘  │
│  ┌────────────▼──────────────────────────────┐  │
│  │ AgentRunner interface                     │  │
│  │   APIRunner (v1) | ClaudeCodeRunner (1.5) │  │
│  └────────────┬──────────────────────────────┘  │
│  ┌────────────▼──────────────────────────────┐  │
│  │ Tools: web_search, fs, bash (sandboxed),  │  │
│  │        test_runner, inbox_io              │  │
│  └────────────┬──────────────────────────────┘  │
│  ┌────────────▼──────────────────────────────┐  │
│  │ Sandbox (Docker container per project)    │  │
│  └───────────────────────────────────────────┘  │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│ Project Directory (user's filesystem)           │
│  • source files (what the team builds)          │
│  • .devteam/                                    │
│     • plan.md                                   │
│     • tasks.json                                │
│     • decisions.log                             │
│     • inboxes/                                  │
│     • agent_log.jsonl                           │
└─────────────────────────────────────────────────┘
```

## State Machine

The orchestrator is a state machine over project state. States:

- `INIT` — project created, awaiting configuration (budget, limits)
- `INTERVIEW` — Architect is interviewing the user
- `PLANNING` — Architect is drafting plan + MVP spec
- `AWAIT_APPROVAL` — plan written, user must approve
- `DISPATCHING` — Dispatcher is decomposing a phase into tasks
- `EXECUTING` — Coder is working on a task; Reviewer may be engaged
- `PHASE_REVIEW` — phase complete, user must approve before next phase
- `PAUSED` — user paused; no agents running
- `BLOCKED` — escalation triggered, awaiting user or Architect replanning
- `COMPLETE` — MVP achieved per spec
- `FAILED` — budget exceeded or unrecoverable error

Transitions are logged to `decisions.log` with timestamps and reasons.

## Shared Artifacts

All state lives under `.devteam/` inside the user's project directory.

### `plan.md`
Markdown document authored by Architect, approved by user. Contains:
- Project summary and vision
- Target user and use cases
- MVP scope (what's in, what's out)
- Phases (P1, P2, …), each with goals and acceptance criteria
- Tech stack decisions and rationale
- Non-goals
- Known risks and open questions

### `tasks.json`
Dispatcher-generated, one entry per task:
```json
{
  "id": "P1-T3",
  "phase": "P1",
  "title": "Implement user signup endpoint",
  "description": "...",
  "acceptance_criteria": ["POST /signup returns 201 on valid input", "..."],
  "dependencies": ["P1-T1", "P1-T2"],
  "status": "pending | in_progress | review | blocked | done",
  "assigned_to": "coder",
  "iterations": 0,
  "budget_tokens": 50000,
  "notes": []
}
```

### `decisions.log`
Append-only newline-delimited JSON. Every significant agent decision, state transition, research finding, and escalation. The audit trail.

### `inboxes/<role>.json`
Messages queued for an agent by other agents or the user. Agents check their inbox at turn boundaries.

### `agent_log.jsonl`
Streaming log of agent activity — tool calls, tokens used, wall clock time. Feeds the frontend activity panel.

## AgentRunner Interface

The single abstraction that lets us swap backends:

```python
class AgentRunner(Protocol):
    async def run(
        self,
        role: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[Tool],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> AgentRunResult: ...
```

`APIRunner` implements this via the Anthropic Python SDK. `ClaudeCodeRunner` (v1.5) will implement it by driving a Claude Code session.

## Budgets and Limits

Set by user during project setup. Enforced by orchestrator:

- Per-task token budget (default 50k)
- Per-phase token budget
- Project total token budget
- Max iterations per task before forced escalation (default 5)
- Max wall clock per task
- Max wall clock per project (e.g., "work for 1 hour then stop")

When a budget is exceeded, the orchestrator moves the project to `BLOCKED` and surfaces it to the user.
