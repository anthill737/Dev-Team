# Getting Started

This guide walks you through running Dev Team v1 locally for the first time.

## Prerequisites

You need two things installed on your machine. Both have "install for this user only" options, so **you do not need admin rights** — important on work or school computers.

1. **Python 3.11 or newer** — https://www.python.org/downloads/
   - On the first install screen, **check "Add python.exe to PATH"** at the bottom. Critical.
   - Choose "Install Now" (user-level install, no admin needed).

2. **Node.js 20 LTS or newer** — https://nodejs.org/
   - Download the "LTS" installer.
   - If the standard installer asks for admin rights, you can use [fnm](https://github.com/Schniz/fnm) instead (per-user, no admin).

3. **An Anthropic API key** — https://console.anthropic.com. Create an account, add a payment method (required) and a few dollars of credits, then create an API key. Your key is kept in memory on your machine only — it is never written to disk.

## First Run — Windows (one-click)

1. Extract the archive to a folder somewhere sensible (e.g. `C:\Users\you\dev-team`).
2. Double-click **`Start Dev Team.bat`** inside that folder.
3. Windows may warn that the file is from an unknown publisher — click "More info" → "Run anyway". If Windows says the file is "blocked," right-click it → Properties → check "Unblock" at the bottom → OK.
4. A terminal window opens and walks you through the setup. It will:
   - Check that Python and Node are installed.
   - Create a Python virtual environment and install backend dependencies.
   - Install frontend dependencies via `npm install`.
   - Start both the backend (port 8000) and frontend (port 3000) as background processes.
   - Open http://localhost:3000 in your default browser when ready.
5. First run takes 2-4 minutes because it's installing dependencies. Subsequent runs take about 5 seconds.
6. The terminal window is safe to close once you see "Dev Team is running" — the backend and frontend keep running in the background.
7. To stop, double-click **`Stop Dev Team.bat`** in the same folder.

## First Run — Mac / Linux

The one-click launcher is Windows-only. Mac/Linux users run the components manually for now:

```bash
# Backend
cd dev-team/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Frontend (in a second terminal)
cd dev-team/frontend
npm install
npm run dev
```

Open http://localhost:3000 when both are ready.

1. **Paste your API key.** It's validated against Anthropic's API and stored in memory.
2. **Create a project.** Pick a name, point it at a directory (absolute path) on your machine — for example `C:\Users\you\code\my-app` on Windows, or `/Users/you/code/my-app` on Mac. If the directory doesn't exist, it'll be created. Set your budget and time limit.
3. **Talk to the Architect.** Describe what you want to build. The Architect will ask questions, research similar projects on the web, and push back when your ideas need sharpening. Every search and key decision is logged to the Decisions panel on the right.
4. **Review the plan.** When the Architect is satisfied, it writes `plan.md` to your project's `.devteam/` directory. You approve it, or send it back with feedback.

## What v1 Can Do

- Interview you about your project as a senior engineer
- Research the web for similar products, common architectures, and pitfalls
- Draft a plan and MVP specification
- Accept or reject feedback and revise

## What v1 Can't Do Yet

- **Actually write code.** The Dispatcher → Coder → Reviewer execution loop is the next build
  phase. v1 is the architect layer; v2 adds the dev team that executes.
- **Run continuously while your computer sleeps.** Paused projects resume when you return.
- **Use your Max subscription.** v1 is API-only; subscription support (via Claude Code) is
  planned for v1.5.

## Where Your Data Lives

Everything the system tracks lives in your project directory under `.devteam/`:

- `plan.md` — the plan the Architect wrote, approved by you
- `meta.json` — project status, budgets, phase progress
- `interview.jsonl` — full Architect ↔ you conversation
- `decisions.log` — append-only audit trail of agent research and decisions
- `tasks.json` — task queue (populated in v2)
- `inboxes/` — inter-agent messages (populated in v2)

Nothing about your project is sent to any service other than the Anthropic API (for model
calls) and DuckDuckGo (for web research by the Architect).

## Troubleshooting

**"Python is not installed (or not on PATH)".** You either don't have Python, or you installed it without checking "Add python.exe to PATH". Reinstall from https://www.python.org/downloads/ and **check that box** on the first screen of the installer. Then restart your terminal.

**"Node.js is not installed (or not on PATH)".** Same idea — install Node 20 LTS from https://nodejs.org/. Restart your terminal after.

**Backend did not respond within 60 seconds.** Check `.devteam-run\backend.log` and `.devteam-run\backend.err.log` in the project folder. Common causes: a Python dependency failed to install (often an issue with corporate proxy or TLS), port 8000 is already in use by something else, or a firewall is blocking localhost connections.

**Frontend did not respond within 60 seconds.** Check `.devteam-run\frontend.log`. Usually npm install had an issue, or port 3000 is in use.

**"Could not create project directory".** Pick a path you can write to — somewhere in your user folder like `C:\Users\you\code\my-app` rather than `C:\Program Files\...`.

**WebSocket disconnects immediately.** Usually means the backend restarted. Your API key is kept in memory only — re-enter it in the UI.

**Corporate proxy or TLS inspection breaks `pip install` or `npm install`.** Many work networks intercept HTTPS traffic. Configure pip and npm to trust your company's CA certificate, or run first-time setup from home where the network is clean. After deps are installed, Dev Team only talks to the Anthropic API at runtime.

**The Architect seems to refuse to stop asking questions.** That's by design — it's instructed to interview until it has enough for a concrete plan. You can tell it explicitly "I think you have enough, please draft the plan" and it will re-evaluate.

**Token usage climbing fast.** Claude Opus (default for Architect and Reviewer) is the most expensive tier. If cost matters more than quality for early experiments, set `DEVTEAM_MODEL_ARCHITECT=<a sonnet model id>` in a `.env` file next to `Start Dev Team.bat`.
