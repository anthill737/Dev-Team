// StatusBar — persistent indicator at the top of the workspace showing what
// the system is currently doing. Solves the "did it hang?" problem the user
// hit after clicking Approve: the input went grey with no obvious signal
// that the Dispatcher was (supposed to be) picking things up.
//
// Always renders in one of three modes:
//   • ACTIVE   — something is running right now (pulse + description)
//   • WAITING  — waiting for user input (clear CTA)
//   • IDLE     — at rest (neutral state readout)
//
// Every status string is mapped here, including the "v2 not built yet" cases
// so the user sees honest copy instead of vague silence.

import type { ProjectDetail, ProjectStatus } from "../lib/types";

interface Props {
  project: ProjectDetail | null;
  agentStreaming: boolean;
  agentCurrentActivity?: string | null;
  /** When project is BLOCKED, the reason string from the latest relevant
   *  decisions.log entry. Surfaced directly so the user doesn't have to dig. */
  blockedReason?: string | null;
  /** When the block is dispatcher-related, parent provides a retry callback
   *  and StatusBar shows a button. Parent is responsible for deciding when
   *  retry is actually appropriate. */
  onRetryDispatcher?: () => void;
  retryingDispatcher?: boolean;
  /** When a task is blocked (Coder exhausted budget, iteration cap, etc.), parent
   *  provides a resume callback that resets the offending task to pending and
   *  kicks the execution loop back on. */
  onResumeExecution?: () => void;
  resumingExecution?: boolean;
  /** When project is complete, parent provides a callback that flips the project
   *  back to interview so the user can request additional work. Appears as an
   *  "Add more work" button in the status bar. */
  onAddWork?: () => void;
  addingWork?: boolean;
  /** Pause/resume during execution. Pause takes effect between tasks — the
   *  currently-running Coder or Reviewer will finish first, then the loop halts.
   *  Close the backend window for a hard stop that doesn't wait. */
  onPause?: () => void;
  pausing?: boolean;
  onResumePaused?: () => void;
  resumingPaused?: boolean;
  /** Open the settings modal (name, path, budgets, OS, etc). Gear button appears
   *  whenever this callback is provided. Safe in any project state — the modal
   *  itself gates dangerous edits (like root_path while running). */
  onOpenSettings?: () => void;
  /** Force-submit the current plan.md for user approval — backup for when the
   *  Architect is stuck logging 'handing off' without calling request_approval.
   *  Only shown when project is in INTERVIEW state. */
  onForceSubmitPlan?: () => void;
  forceSubmittingPlan?: boolean;
  /** Open the project root in Explorer / VS Code / Terminal. Always shown
   *  when callback provided — quick launchers users will want frequently. */
  onOpenIn?: (target: "explorer" | "vscode" | "terminal") => void;
}

type StatusMode = "active" | "waiting" | "idle" | "error";

interface StatusDisplay {
  mode: StatusMode;
  label: string;
  detail: string;
}

export function StatusBar({
  project,
  agentStreaming,
  agentCurrentActivity,
  blockedReason,
  onRetryDispatcher,
  retryingDispatcher,
  onResumeExecution,
  resumingExecution,
  onAddWork,
  addingWork,
  onPause,
  pausing,
  onResumePaused,
  resumingPaused,
  onOpenSettings,
  onForceSubmitPlan,
  forceSubmittingPlan,
  onOpenIn,
}: Props) {
  const s = computeStatus(project, agentStreaming, agentCurrentActivity, blockedReason);

  const colorByMode: Record<StatusMode, string> = {
    active: "bg-amber-900/30 border-amber-700/60 text-amber-200",
    waiting: "bg-blue-900/30 border-blue-700/60 text-blue-200",
    idle: "bg-panel border-line text-gray-400",
    error: "bg-red-900/40 border-red-700/60 text-red-200",
  };

  return (
    <div
      className={`border-b px-4 py-2 flex items-center gap-3 ${colorByMode[s.mode]}`}
      role="status"
      aria-live="polite"
    >
      <StatusIndicator mode={s.mode} />
      <div className="flex-1 min-w-0">
        <div className="text-[17px] font-medium truncate">{s.label}</div>
        {s.detail && <div className="text-[15px] opacity-80 truncate">{s.detail}</div>}
      </div>
      {project?.status === "blocked" && onRetryDispatcher && (
        <button
          type="button"
          onClick={onRetryDispatcher}
          disabled={retryingDispatcher}
          className="shrink-0 px-3 py-1 text-[15px] font-medium bg-amber-700 hover:bg-amber-600 disabled:opacity-50 text-white rounded"
        >
          {retryingDispatcher ? "Retrying..." : "Retry Dispatcher"}
        </button>
      )}
      {project?.status === "blocked" && onResumeExecution && (
        <button
          type="button"
          onClick={onResumeExecution}
          disabled={resumingExecution}
          className="shrink-0 px-3 py-1 text-[15px] font-medium bg-amber-700 hover:bg-amber-600 disabled:opacity-50 text-white rounded"
          title="Reset blocked tasks to pending and resume the execution loop."
        >
          {resumingExecution ? "Resuming..." : "Resume execution"}
        </button>
      )}
      {project?.status === "complete" && onAddWork && (
        <button
          type="button"
          onClick={onAddWork}
          disabled={addingWork}
          className="shrink-0 px-3 py-1 text-[15px] font-medium bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white rounded"
          title="Reopen this project to add a new feature, fix, or refactor. The Architect will interview you about the incremental work and append a new phase to the plan."
        >
          {addingWork ? "Opening..." : "Add more work"}
        </button>
      )}
      {(project?.status === "executing" ||
        project?.status === "dispatching" ||
        project?.status === "awaiting_task_review") &&
        onPause && (
          <button
            type="button"
            onClick={onPause}
            disabled={pausing}
            className="shrink-0 px-3 py-1 text-[15px] font-medium bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white rounded"
            title="Pause between tasks. The currently running Coder or Reviewer will finish first, then the loop halts. Close the backend window for a hard stop."
          >
            {pausing ? "Pausing..." : "Pause"}
          </button>
        )}
      {project?.status === "paused" && onResumePaused && (
        <button
          type="button"
          onClick={onResumePaused}
          disabled={resumingPaused}
          className="shrink-0 px-3 py-1 text-[15px] font-medium bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white rounded"
          title="Resume the execution loop. It picks up at the next task."
        >
          {resumingPaused ? "Resuming..." : "Resume"}
        </button>
      )}
      {project?.status === "interview" && onForceSubmitPlan && (
        <button
          type="button"
          onClick={onForceSubmitPlan}
          disabled={forceSubmittingPlan}
          className="shrink-0 px-3 py-1 text-[15px] font-medium bg-orange-800 hover:bg-orange-700 disabled:opacity-50 text-white rounded"
          title="If the Architect is stuck logging 'handing off' without calling request_approval, this bypasses it and submits whatever's in plan.md for your approval. Requires plan.md to have content — edit it manually first if needed."
        >
          {forceSubmittingPlan ? "Submitting..." : "Force submit plan"}
        </button>
      )}
      {onOpenIn && (
        <>
          {/* Quick launchers — always visible in the workspace so the user can
              jump to Explorer / VS Code / Terminal without leaving Dev Team.
              Saves a CD into the project folder every time. */}
          <button
            type="button"
            onClick={() => onOpenIn("explorer")}
            className="shrink-0 px-2 py-1 text-[17px] text-gray-400 hover:text-gray-200 border border-line hover:border-gray-600 rounded"
            title="Open the project folder in Windows Explorer."
            aria-label="Open in Explorer"
          >
            📁
          </button>
          <button
            type="button"
            onClick={() => onOpenIn("vscode")}
            className="shrink-0 px-2 py-1 text-[15px] font-semibold text-gray-400 hover:text-gray-200 border border-line hover:border-gray-600 rounded"
            title="Open the project in VS Code. Requires the `code` command on PATH."
            aria-label="Open in VS Code"
          >
            VS
          </button>
          <button
            type="button"
            onClick={() => onOpenIn("terminal")}
            className="shrink-0 px-2 py-1 text-[15px] font-mono text-gray-400 hover:text-gray-200 border border-line hover:border-gray-600 rounded"
            title="Open a fresh terminal already CDed into the project folder."
            aria-label="Open terminal"
          >
            {">_"}
          </button>
        </>
      )}
      {onOpenSettings && (
        <button
          type="button"
          onClick={onOpenSettings}
          className="shrink-0 px-2 py-1 text-[17px] text-gray-400 hover:text-gray-200 border border-line hover:border-gray-600 rounded"
          title="Project settings — name, path, budgets, OS. Safe to open any time."
          aria-label="Open project settings"
        >
          ⚙
        </button>
      )}
    </div>
  );
}

function StatusIndicator({ mode }: { mode: StatusMode }) {
  if (mode === "active") {
    return (
      <div className="relative h-3 w-3 flex-shrink-0">
        <div className="absolute inset-0 rounded-full bg-amber-400 animate-ping opacity-70" />
        <div className="absolute inset-0 rounded-full bg-amber-400" />
      </div>
    );
  }
  if (mode === "waiting") {
    return <div className="h-3 w-3 flex-shrink-0 rounded-full bg-blue-400" />;
  }
  if (mode === "error") {
    return <div className="h-3 w-3 flex-shrink-0 rounded-full bg-red-400" />;
  }
  return <div className="h-3 w-3 flex-shrink-0 rounded-full bg-gray-500" />;
}

function computeStatus(
  project: ProjectDetail | null,
  streaming: boolean,
  activity?: string | null,
  blockedReason?: string | null,
): StatusDisplay {
  if (!project) {
    return {
      mode: "idle",
      label: "Loading project...",
      detail: "",
    };
  }

  // If an agent is actively mid-turn, that trumps the project status
  if (streaming) {
    return {
      mode: "active",
      label: "Architect is working",
      detail: activity || "Composing response, running tools, researching...",
    };
  }

  const status: ProjectStatus = project.status;

  switch (status) {
    case "init":
      return {
        mode: "waiting",
        label: "Ready to start",
        detail: "Describe your project in the chat to begin the interview.",
      };

    case "interview":
      return {
        mode: "waiting",
        label: "Your turn",
        detail: "The Architect is waiting for your next answer.",
      };

    case "planning":
      return {
        mode: "active",
        label: "Architect is drafting the plan",
        detail: "This may take a moment — writing plan.md.",
      };

    case "await_approval":
      return {
        mode: "waiting",
        label: "Plan ready for your review",
        detail: "Approve or request changes in the Plan panel (middle column).",
      };

    case "dispatching":
      return {
        mode: "active",
        label: "Dispatcher is decomposing the plan into tasks",
        detail: "Watch the Tasks panel for incoming items.",
      };

    case "executing":
      return {
        mode: "active",
        label: "Dev team is building",
        detail: "Coder and Reviewer are working through tasks.",
      };

    case "awaiting_task_review":
      return {
        mode: "waiting",
        label: "A task needs your review",
        detail: "See the amber panel above — verify the work, then approve or request changes.",
      };

    case "phase_review":
      return {
        mode: "waiting",
        label: "Phase complete — your approval needed",
        detail: "Review the phase output and approve the next phase to begin.",
      };

    case "paused":
      return {
        mode: "idle",
        label: "Paused",
        detail: "Resume from the project header to continue.",
      };

    case "blocked":
      return {
        mode: "error",
        label: "Blocked — attention needed",
        detail:
          blockedReason ||
          "An agent escalated or a budget was exceeded. Check the Decisions log.",
      };

    case "complete":
      return {
        mode: "idle",
        label: "Project complete",
        detail: "The MVP is done per the approved plan.",
      };

    case "failed":
      return {
        mode: "error",
        label: "Project failed",
        detail: "Check the Decisions log for the cause.",
      };
  }
}
