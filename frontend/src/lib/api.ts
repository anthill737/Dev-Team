// HTTP API client — thin wrapper around fetch, typed against the backend.

import type {
  DecisionEntry,
  InterviewTurn,
  ProjectDetail,
  ProjectSummary,
  Task,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore parse errors */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --- Session ---------------------------------------------------------------

export async function setApiKey(apiKey: string): Promise<void> {
  await request<{ ok: boolean }>("/api/session/key", {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey }),
  });
}

export interface SessionStatus {
  has_key: boolean;
  // Which agent runner the backend uses. "claude_code" means the user's
  // subscription via Claude Code CLI (no API key required); "api" means
  // per-token billing via Anthropic API key.
  runner: "claude_code" | "api";
  runner_description: string;
}

export async function sessionStatus(): Promise<SessionStatus> {
  return request<SessionStatus>("/api/session/status");
}

export async function clearApiKey(): Promise<void> {
  await request("/api/session/key", { method: "DELETE" });
}

// --- Projects --------------------------------------------------------------

export interface CreateProjectArgs {
  name: string;
  root_path: string;
  project_token_budget?: number;
  default_task_token_budget?: number;
  max_task_iterations?: number;
  max_wall_clock_seconds?: number | null;
  // Per-agent model overrides. Omit any to use the global default. Send the
  // sentinel "default" to explicitly clear an existing override (mainly used
  // by the update endpoint, but harmless on create — same effect as omit).
  model_architect?: string;
  model_dispatcher?: string;
  model_coder?: string;
  model_reviewer?: string;
  // Browser-based runtime verification. Default false (off). When true, the
  // Reviewer's Rule 3 verification path uses playwright_check.
  playwright_enabled?: boolean;
}

export async function createProject(args: CreateProjectArgs): Promise<ProjectDetail> {
  return request<ProjectDetail>("/api/projects", {
    method: "POST",
    body: JSON.stringify(args),
  });
}

export async function listProjects(): Promise<ProjectSummary[]> {
  return request<ProjectSummary[]>("/api/projects");
}

export async function getProject(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}`);
}

export async function getPlan(id: string): Promise<string> {
  const r = await request<{ content: string }>(`/api/projects/${id}/plan`);
  return r.content;
}

export async function getInterview(id: string): Promise<InterviewTurn[]> {
  return request<InterviewTurn[]>(`/api/projects/${id}/interview`);
}

export async function getTasks(id: string): Promise<Task[]> {
  return request<Task[]>(`/api/projects/${id}/tasks`);
}

export async function getDecisions(id: string, limit = 100): Promise<DecisionEntry[]> {
  return request<DecisionEntry[]>(`/api/projects/${id}/decisions?limit=${limit}`);
}

export async function decidePlan(
  id: string,
  approved: boolean,
  feedback?: string,
): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/plan/decision`, {
    method: "POST",
    body: JSON.stringify({ approved, feedback }),
  });
}

export async function pauseProject(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/pause`, { method: "POST" });
}

export async function resumePausedProject(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/resume`, { method: "POST" });
}

export async function retryDispatcher(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/retry_dispatcher`, {
    method: "POST",
  });
}

export async function resumeExecution(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/resume_execution`, {
    method: "POST",
  });
}

export async function addWork(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/add_work`, {
    method: "POST",
  });
}

// Backup for Architect-stuck situations: force plan.md into AWAIT_APPROVAL
// without waiting for the Architect to call request_approval. Use this when
// the Architect is caught in a handoff-narration loop. Backend refuses if the
// project isn't in INTERVIEW or plan.md is effectively empty.
export async function forceSubmitPlan(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/force_submit_plan`, {
    method: "POST",
  });
}

// Rescue projects that hit the multi-phase auto-advance bug: the project is
// marked COMPLETE but plan.md has phases that were never decomposed. This
// finds the first undone phase in plan.md, sets it as current_phase, and
// flips status to DISPATCHING. The existing pipeline then takes over from
// there. Backend refuses if all phases are already done, the project is
// currently running, or tasks.json already has tasks for the phase to
// resume (partial-dispatch conflict the user should investigate manually).
export async function resumePhases(id: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}/resume_phases`, {
    method: "POST",
  });
}

// Open the project's root directory in a chosen tool: Explorer, VS Code, or
// Terminal. The backend shells out to the OS (os.startfile on Windows, open
// on macOS, xdg-open / specific terminal emulators on Linux). Fire-and-forget;
// we don't track the launched process.
export async function openProjectIn(
  id: string,
  target: "explorer" | "vscode" | "terminal",
): Promise<{ ok: boolean; target: string; path: string; message: string }> {
  return request(`/api/projects/${id}/open`, {
    method: "POST",
    body: JSON.stringify({ target }),
    headers: { "Content-Type": "application/json" },
  });
}

// --- Agent inspector ----------------------------------------------------------
// Per-agent live transcript data. Backend captures every StreamEvent flowing
// through the orchestrator (Architect, Dispatcher, Coder, Reviewer) into an
// in-memory ring buffer. Frontend polls these endpoints to render the
// AgentInspector panel.

export type AgentRole =
  | "architect"
  | "dispatcher"
  | "coder"
  | "reviewer"
  | "orchestrator";

export interface AgentEvent {
  agent: AgentRole;
  kind: string;
  // Payload shape depends on kind. Examples:
  //   text_delta   → { text: "..." }
  //   tool_use_start → { name, input, id }
  //   tool_result  → { tool_use_id, content, is_error }
  //   usage        → { input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens }
  //   turn_complete → { stop_reason }
  payload: Record<string, unknown>;
  timestamp: number;
  seq: number;
  task_id: string | null;
}

export interface AgentEventsResponse {
  events: AgentEvent[];
  latest_seq: number;
}

export interface AgentSummaryEntry {
  event_count: number;
  last_seq: number;
  last_ts: number;
  last_kind: string | null;
}

export interface AgentSummaryResponse {
  agents: Record<AgentRole, AgentSummaryEntry>;
  latest_seq: number;
}

export async function getAgentEvents(
  id: string,
  agent?: AgentRole,
  since: number = 0,
): Promise<AgentEventsResponse> {
  const qs = new URLSearchParams();
  if (agent) qs.set("agent", agent);
  qs.set("since", String(since));
  return request<AgentEventsResponse>(
    `/api/projects/${id}/agent_events?${qs.toString()}`,
  );
}

export async function getAgentSummary(
  id: string,
): Promise<AgentSummaryResponse> {
  return request<AgentSummaryResponse>(
    `/api/projects/${id}/agent_events/summary`,
  );
}

// Delete a project. `purge` controls whether to also remove the project's
// .devteam/ state folder. User code outside .devteam/ is NEVER touched.
// Backend refuses to delete a project with a running execution job.
export async function deleteProject(
  id: string,
  purge: boolean = false,
): Promise<void> {
  await request<unknown>(
    `/api/projects/${id}?purge=${purge ? "true" : "false"}`,
    { method: "DELETE" },
  );
}

// Partial update of project settings. Only fields explicitly included are
// applied; omitted fields stay as-is. Use `clear_max_wall_clock: true` to
// flip wall clock from a limit back to "unlimited".
//
// root_path changes have stricter rules: project must not be running, and
// the new path must already have .devteam/meta.json matching this project id.
export interface ProjectUpdateArgs {
  name?: string;
  root_path?: string;
  project_token_budget?: number;
  default_task_token_budget?: number;
  max_task_iterations?: number;
  max_wall_clock_seconds?: number;
  clear_max_wall_clock?: boolean;
  user_platform?: "windows" | "macos" | "linux";
  // Per-agent model overrides. Send a model string to set; send the sentinel
  // "default" to clear an existing override and revert to global default;
  // omit the field entirely to leave it unchanged.
  model_architect?: string;
  model_dispatcher?: string;
  model_coder?: string;
  model_reviewer?: string;
  // Browser-based runtime verification. Omit to leave unchanged; explicit
  // true/false to flip. Backend persists this to meta and the Reviewer
  // reads the new value on its next run.
  playwright_enabled?: boolean;
}

// Catalog of available models + current global defaults per role. Frontend
// fetches this once on app load and uses it to populate the model-picker
// dropdowns. Avoids hardcoding the list and keeps it in sync with backend.
export interface ModelChoice {
  string: string;
  label: string;
  cost_hint: string;
}

export interface ModelCatalog {
  choices: ModelChoice[];
  defaults: { architect: string; dispatcher: string; coder: string; reviewer: string };
}

export async function getModelCatalog(): Promise<ModelCatalog> {
  return request<ModelCatalog>("/api/projects/models/catalog");
}

export async function updateProject(
  id: string,
  args: ProjectUpdateArgs,
): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/api/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(args),
  });
}

// Edit a single task. Safe to call while the task is running — the Coder
// re-reads the task at the start of each iteration, so budget bumps or
// appended notes take effect on the next iteration.
//
// `interrupt`: when paired with add_note, halts the execution loop at the
// task boundary and surfaces the note as a user-review moment. Use for
// "stop and show me X" notes the Coder shouldn't just pick up passively.
export async function updateTask(
  projectId: string,
  taskId: string,
  args: { budget_tokens?: number; add_note?: string; interrupt?: boolean },
): Promise<Task> {
  return request<Task>(`/api/projects/${projectId}/tasks/${taskId}`, {
    method: "PATCH",
    body: JSON.stringify(args),
  });
}

// Apply a new budget to every non-done task in one call. Useful when the
// user realizes mid-project that defaults were too low.
export async function bulkUpdateTaskBudget(
  projectId: string,
  budgetTokens: number,
): Promise<{ updated: number; task_ids: string[] }> {
  return request<{ updated: number; task_ids: string[] }>(
    `/api/projects/${projectId}/tasks/bulk_budget`,
    {
      method: "POST",
      body: JSON.stringify({ budget_tokens: budgetTokens }),
    },
  );
}

export async function reviewTask(
  projectId: string,
  taskId: string,
  approved: boolean,
  feedback?: string,
): Promise<ProjectDetail> {
  return request<ProjectDetail>(
    `/api/projects/${projectId}/tasks/${taskId}/review`,
    {
      method: "POST",
      body: JSON.stringify({ approved, feedback }),
    },
  );
}
