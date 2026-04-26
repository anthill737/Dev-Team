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
  // True when a background execution job is actively running. Distinct from
  // status (which is on-disk state): a project can have status "executing"
  // but no running job (e.g. backend was restarted mid-execution).
  is_running?: boolean;
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
  // Platform the user is on ("windows" | "macos" | "linux"). Set at project
  // creation from the backend host; used to pick shell-syntax hints in
  // Coder/Reviewer prompts. Editable via the project settings UI.
  user_platform?: "windows" | "macos" | "linux";
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
  // Resolved per-agent model assignments — backend returns the override OR
  // the global default, never null. UI uses these to populate the settings
  // dropdowns. Strings match what's in the model catalog.
  model_architect: string;
  model_dispatcher: string;
  model_coder: string;
  model_reviewer: string;
  // Raw overrides — null when no per-project override is set. Frontend uses
  // these to distinguish "user explicitly picked Sonnet" from "no override
  // set; happens to match global default of Sonnet". When null, show
  // "(default)" in the dropdown; when set, show the specific model.
  override_model_architect: string | null;
  override_model_dispatcher: string | null;
  override_model_coder: string | null;
  override_model_reviewer: string | null;
  // Phase IDs from plan.md not yet marked done in meta.phases. Empty list
  // means the project is fully executed (or no plan yet). UI uses this to
  // conditionally show "Resume phases" — visible only when there are
  // unfinished plan phases.
  unresolved_phase_ids: string[];
  // Browser-based runtime verification toggle. When true, the Reviewer uses
  // playwright_check for browser-rendered artifacts. UI surfaces this as a
  // status indicator and a checkbox in CreateProject + EditProject modals.
  playwright_enabled: boolean;
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
  // True when the user used the "Save and interrupt" action to halt the task
  // mid-execution. The review panel treats approve/reject differently for
  // interrupted tasks: approve resumes rather than marking done.
  interrupted_by_user?: boolean;
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
  // Reviewer agent (distinct from user-review above). When Dispatcher flags a
  // task with requires_review, a skeptical Reviewer runs after the Coder signals
  // done. review_cycles counts how many times the Reviewer has rejected. If the
  // Reviewer rejects at max cycles, the task is blocked and review_findings
  // carries the specific issues the user needs to resolve.
  requires_review?: boolean;
  review_cycles?: number;
  review_findings?: string[];
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
  | { type: "loop_safety_halt" }
  // --- Reviewer gate events (emitted when a task is flagged requires_review)
  | {
      type: "review_start";
      task_id: string;
      cycle: number;
      max_cycles: number;
    }
  | { type: "review_approved"; task_id: string; summary: string }
  | {
      type: "review_request_changes";
      task_id: string;
      cycle: number;
      findings: string[];
    }
  | {
      type: "review_outcome";
      result_kind: "approve" | "request_changes" | "error" | null;
      summary: string;
      findings: string[];
    };
