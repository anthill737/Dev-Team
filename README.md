# Dev Team

An autonomous software development team powered by Claude. You describe what you want to build; a team of specialized agents — Architect, Dispatcher, Coder, Reviewer — interviews you, plans the project, and builds it.

## How It Works

1. **Interview.** The Architect, acting as a senior engineer, interviews you about the project. It asks questions, researches similar products, pushes back on risky choices, and drafts a plan and MVP specification.
2. **Approval.** You review the plan. You approve, request changes, or redirect.
3. **Build.** The dev team executes the plan phase by phase. The Dispatcher breaks phases into tasks. The Coder implements each task. The Reviewer audits the output against acceptance criteria. Tasks escalate back to the Architect when the plan turns out to be wrong.
4. **Checkpoints.** You approve each phase transition. You can leave messages for the team at any time via an inbox the agents check between tasks.

## What's Built (v1)

- Web app you run locally (`docker compose up`)
- Architect interview with web research and reflective practice protocol
- Plan document generation, user approval flow
- Dispatcher → Coder → Reviewer execution loop
- Docker sandbox for safe code execution
- Monaco-based embedded IDE for watching the work
- Bring-your-own Anthropic API key

## What's Coming Later

- Claude Code / Max subscription backend (v1.5)
- Dynamic specialist agent spawning (v2)
- Cloud execution for 24/7 autonomous operation (v2)
- Native GitHub repo integration (v1.5)

## Setup

Prerequisites: **Python 3.11+** and **Node.js 18+** (20 LTS recommended). Both have "install for this user only" options that don't need admin rights, which is ideal on work machines where Docker isn't available.

Plus an Anthropic API key from [console.anthropic.com](https://console.anthropic.com).

**Windows — double-click to launch:**

1. Extract the archive.
2. Double-click **`Start Dev Team.bat`**.
3. Wait for the terminal to say "Dev Team is running" (2-4 min first time while it installs dependencies, ~5 sec after).
4. Your browser opens automatically at http://localhost:3939.

To stop: double-click **`Stop Dev Team.bat`**.

**Mac / Linux (manual):**

```bash
cd dev-team/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8000 &

cd ../frontend
npm install
npm run dev
```

Open http://localhost:3939.

In either case: paste your Anthropic API key, pick a project directory, and start.

### Skip the API key prompt (recommended)

You can persist your API key so you aren't prompted every time you start the app. Two options:

**Option A (easiest): drop it in a `.env` file.** Copy `.env.example` to `.env` in the project root, uncomment the `ANTHROPIC_API_KEY` line, and paste your key. The launcher loads this automatically. Since `.env` is in `.gitignore`, it won't be committed.

**Option B: set a Windows user environment variable.**
1. Win+R → `sysdm.cpl` → Advanced → Environment Variables
2. Under "User variables", click New
3. Variable name: `ANTHROPIC_API_KEY`, Variable value: your key
4. Restart the Dev Team launcher

Either way, on next startup you'll skip the key-entry screen and go straight to your projects.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.
See [docs/PROMPTS.md](docs/PROMPTS.md) for the system prompts, including the reflective practice protocol every agent follows.

### Smoke tests (real API)

`backend/smoke_dispatcher.py` drives the Dispatcher end-to-end against the real Anthropic API with a realistic fixture plan. Run it when you've changed prompts, tools, or anything that affects model behavior:

```bash
cd backend
ANTHROPIC_API_KEY=sk-ant-... python smoke_dispatcher.py
```

Typical run is 40-60 seconds and costs a few cents. A run that takes >3 minutes or hits max_tokens indicates a prompt problem. Unit tests can't catch this class of bug because scripted runners don't exercise the model.

## License

TBD
