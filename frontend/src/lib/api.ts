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

export async function sessionStatus(): Promise<{ has_key: boolean }> {
  return request<{ has_key: boolean }>("/api/session/status");
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
