// Types mirroring the backend API shapes.

export type ProjectStatus =
  | "init"
  | "interview"
  | "planning"
  | "await_approval"
  | "dispatching"
  | "executing"
  | "awaiting_task_review"
  | "phase_review"
  | "paused"
  | "blocked"
  | "complete"
  | "failed";

export interface ProjectSummary {
  id: string;
  name: string;
  root_path: string;
  status: ProjectStatus;
  created_at: number;
  tokens_used: number;
  tasks_completed: number;
}

export interface ProjectPhase {
  id: string;
  title: string;
  status: string;
  approved_by_user: boolean;
}

export interface ProjectDetail extends ProjectSummary {
  project_token_budget: number;
  default_task_token_budget: number;
  max_task_iterations: number;
  max_wall_clock_seconds: number | null;
  current_phase: string | null;
  phases: ProjectPhase[];
  // Per-model token tracking for cost display. Default 0 for older projects.
  tokens_input_opus?: number;
  tokens_output_opus?: number;
  tokens_input_sonnet?: number;
  tokens_output_sonnet?: number;
  tokens_input_haiku?: number;
  tokens_output_haiku?: number;
  // Cache breakdown (subset of tokens_input_*). Reads bill at 10% of input,
  // writes at 125%. Lets the UI show cache effectiveness.
  cache_read_opus?: number;
  cache_creation_opus?: number;
  cache_read_sonnet?: number;
  cache_creation_sonnet?: number;
  cache_read_haiku?: number;
  cache_creation_haiku?: number;
  // Estimated USD cost based on Anthropic public pricing. Backend-computed so the
  // pricing constants live in one place.
  cost_usd_estimate?: number;
}

export interface Task {
  id: string;
  phase: string;
  title: string;
  description: string;
  acceptance_criteria: string[];
  dependencies: string[];
  status: "pending" | "in_progress" | "review" | "blocked" | "done";
  assigned_to: string;
  iterations: number;
  budget_tokens: number;
  notes: string[];
  // Populated when the task is marked done by the execution loop.
  // Older tasks stored before this field existed may not have them.
  summary?: string;
  completed_at?: number;
  // Populated when the Coder signals needs_user_review and the task is awaiting
  // user verification. Cleared when user rejects (sends task back to pending).
  review_summary?: string;
  review_checklist?: string[];
  review_run_command?: string;
  review_files_to_check?: string[];
  review_requested_at?: number;
}

export interface InterviewTurn {
  timestamp: number;
  role: "user" | "assistant";
  content: string;
}

export interface DecisionEntry {
  timestamp: number;
  actor: string;
  kind: string;
  [key: string]: unknown;
}

// WebSocket wire protocol — server → client
export type WsEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_use"; name: string; input: Record<string, unknown> }
  | {
      type: "tool_result";
      name: string;
      is_error: boolean;
      preview: string;
    }
  | { type: "usage"; input_tokens: number; output_tokens: number }
  | { type: "turn_complete"; status: ProjectStatus; tokens_used: number }
  | { type: "error"; message: string }
  // --- Execution loop events (backend passes these through as type=event.kind)
  | { type: "task_start"; task_id: string; iteration: number }
  | {
      type: "scheduler_decision";
      decision_kind: string;
      task_id: string | null;
      reason: string;
    }
  | { type: "phase_complete"; phase: string }
  | { type: "project_complete" }
  | { type: "task_escalated"; task_id: string; reason: string }
  | { type: "task_blocked"; task_id: string; reason: string }
  | { type: "task_needs_user_review"; task_id: string; summary: string }
  | {
      type: "task_outcome";
      outcome_kind: "approved" | "needs_rework" | "needs_user_review" | "blocked" | "failed" | null;
      summary: string;
    }
  | { type: "deadlock"; reason: string }
  | { type: "budget_exceeded"; task_id: string; budget: number }
  | { type: "loop_paused"; reason: string }
  | { type: "loop_exit"; reason: string }
  | { type: "loop_safety_halt" };
