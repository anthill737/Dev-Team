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
