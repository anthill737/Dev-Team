# Dev Team

An autonomous software development harness powered by Claude. You describe what you want to build; a team of specialized agents interviews you, plans the project, and builds it. You review each phase and can send it back for changes.

This is a personal tool — local web app, bring-your-own Anthropic API key, no accounts, no cloud, no telemetry.

## How it works

1. **Interview.** The Architect (Opus) interviews you about what you want to build. Clarifying questions, pushback on risky choices, plan drafting.
2. **Plan approval.** You see the generated plan. Approve or request changes with feedback — the Architect revises and comes back.
3. **Dispatch.** The Dispatcher (Sonnet) decomposes each approved phase into concrete tasks with acceptance criteria. For each task it decides whether a skeptical Reviewer should verify the work.
4. **Execute.** The Coder (Sonnet) works through tasks in dependency order — reads files, writes code, runs tests, iterates until acceptance criteria are met.
5. **Review.** For tasks flagged `requires_review`, a skeptical Reviewer (Opus) verifies the Coder's work before it's marked done. Finds bugs, rejects with specific findings, sends back to Coder. Max 2 review cycles before blocking for you.
6. **Iterate.** When a project completes, you can "Add more work" — the Architect interviews you about additions, appends a new phase to the plan, and the loop continues.


## Requirements

- Windows 10/11 (tested) — launcher scripts are PowerShell; adaptable to Unix but not yet done
- Python 3.11+
- Node.js 20+
- An Anthropic API key (enter it in the app on first run; stored locally at `.devteam-run/api_key`)

## Setup

First-time setup installs the backend venv and frontend dependencies:

```powershell
cd Dev-Team
.\scripts\setup.ps1
```

Then launch with:

```powershell
.\scripts\launch.ps1
```

Two windows open: backend on port 8765, frontend on port 3939. Your browser opens to the frontend automatically. Close either window to stop it.

On first launch, paste your Anthropic API key when prompted. Get one at console.anthropic.com.

## What's in the repo

```
dev-team/
├── backend/          FastAPI server, orchestrator, agents, sandbox
│   ├── app/
│   │   ├── agents/        Architect, Dispatcher, Coder, Reviewer
│   │   ├── api/           HTTP routes + WebSocket endpoints
│   │   ├── orchestrator/  Execution loop, scheduler, task runner
│   │   ├── prompts/       System prompts for each agent
│   │   ├── sandbox/       Process-based sandbox for executing code
│   │   ├── state/         Per-project filesystem-backed state store
│   │   └── tools/         Tool definitions each agent can call
│   └── tests/             134 pytest tests
├── frontend/         Vite + React + TypeScript + Tailwind
│   ├── src/
│   │   ├── components/    Main workspace, chat, task panels, live stream
│   │   ├── hooks/         WebSocket stream hooks
│   │   └── lib/           API client, types
│   └── tests/             Vitest suite
└── scripts/          Launch + setup PowerShell scripts
```

## Models used

- **Architect / Reviewer:** `claude-opus-4-7` — judgment-heavy work
- **Dispatcher / Coder:** `claude-sonnet-4-6` — execution-heavy work

Configurable in `backend/app/config.py`.

## Costs

Prompt caching is enabled on all agent calls (system prompt + tool definitions cached with `cache_control: ephemeral`). A completed small project (Next.js to-do app, 9 tasks) ran around $11 without a Reviewer. With Reviewer enabled on user-facing tasks, expect 20-40% higher costs in exchange for actual verification of behavior instead of trusting self-reported test passes.

Cost estimate is shown live in the workspace header. Hover for cache hit rate.

## State and persistence

Per-project state lives in `<project-path>/.devteam/`:

- `meta.json` — project status, token usage, phase progression
- `plan.md` — the Architect's plan
- `tasks.json` — all tasks across all phases
- `interview.jsonl` — Architect ↔ user chat history
- `decisions.log` — audit trail of every agent decision, tool call, and outcome

App-level state (project registry, API key) lives at `.devteam-run/` next to the backend. This is git-ignored.

## Known limitations

- Reviewer has been unit-tested but has limited real-world exposure. Behavior may surprise you; check `.devteam/decisions.log` for what it flagged.
- Windows-only launcher scripts.
- No cloud execution, no git integration, no multi-user.
- Reviewer doesn't have browser automation (Playwright) yet — it verifies via filesystem reads and running commands, not by clicking through a UI.

## License

No license specified. Personal use.
