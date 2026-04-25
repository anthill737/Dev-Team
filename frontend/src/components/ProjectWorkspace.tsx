import { useCallback, useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import {
  decidePlan,
  getDecisions,
  getInterview,
  getPlan,
  getProject,
  getTasks,
  openProjectIn,
  retryDispatcher,
  resumeExecution,
  addWork,
  pauseProject,
  resumePausedProject,
} from "../lib/api";
import type { DecisionEntry, InterviewTurn, ProjectDetail, Task } from "../lib/types";
import { useArchitectStream } from "../hooks/useArchitectStream";
import { useDispatcherStream } from "../hooks/useDispatcherStream";
import { useExecutionStream } from "../hooks/useExecutionStream";
import { ArchitectChat } from "./ArchitectChat";
import { CompletedTasks } from "./CompletedTasks";
import { DecisionsLog } from "./DecisionsLog";
import { AgentInspector } from "./AgentInspector";
import { PlanViewer } from "./PlanViewer";
import { EditProjectModal } from "./ProjectList";
import { StatusBar } from "./StatusBar";
import { TaskReviewPanel } from "./TaskReviewPanel";
import { TasksView } from "./TasksView";

interface Props {
  projectId: string;
  onBack: () => void;
}

export function ProjectWorkspace({ projectId, onBack }: Props) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [interview, setInterview] = useState<InterviewTurn[]>([]);
  const [plan, setPlan] = useState("");
  const [decisions, setDecisions] = useState<DecisionEntry[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [retryingDispatcher, setRetryingDispatcher] = useState(false);
  const [resumingExecution, setResumingExecution] = useState(false);
  const [addingWork, setAddingWork] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [resumingPaused, setResumingPaused] = useState(false);
  // Project-settings gear in StatusBar opens the same EditProjectModal used on
  // the project list. Keeping the state here (in the workspace) so it can live
  // alongside the other mid-project actions.
  const [showSettings, setShowSettings] = useState(false);

  const refreshProjectData = useCallback(async () => {
    try {
      const [p, i, pl, d, t] = await Promise.all([
        getProject(projectId),
        getInterview(projectId),
        getPlan(projectId),
        getDecisions(projectId, 200),
        getTasks(projectId),
      ]);
      setProject(p);
      setInterview(i);
      setPlan(pl);
      setDecisions(d);
      setTasks(t);
    } catch (e) {
      console.error("Failed to refresh project", e);
    }
  }, [projectId]);

  useEffect(() => {
    refreshProjectData();
  }, [refreshProjectData]);

  useEffect(() => {
    const activeStates = new Set([
      "interview",
      "planning",
      "dispatching",
      "executing",
      "awaiting_task_review",
      "phase_review",
    ]);
    const interval = setInterval(async () => {
      if (!project || activeStates.has(project.status)) {
        try {
          const [p, d] = await Promise.all([
            getProject(projectId),
            getDecisions(projectId, 200),
          ]);
          setProject(p);
          setDecisions(d);
          if (p.status === "await_approval" || p.status === "planning") {
            getPlan(projectId).then(setPlan).catch(() => {});
          }
          // Tasks can appear during dispatching; keep them fresh during exec too
          if (
            p.status === "dispatching" ||
            p.status === "executing" ||
            p.status === "awaiting_task_review" ||
            p.status === "phase_review"
          ) {
            getTasks(projectId).then(setTasks).catch(() => {});
          }
        } catch {
          /* swallow — transient errors are fine during polling */
        }
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [projectId, project?.status, project]);

  const {
    streaming,
    partialText,
    toolActivity,
    tokensThisTurn,
    error,
    send,
    status: wsStatus,
  } = useArchitectStream(projectId, refreshProjectData);

  // Dispatcher + execution now run as one background job (see job_registry on
  // the backend). Dispatcher events flow through the execution WS as part of
  // that stream — no separate dispatcher connection. Keeping the hook instance
  // around but never triggering it so types stay clean; TODO remove the hook
  // entirely in a future pass.
  const dispatcher = useDispatcherStream(projectId, false, refreshProjectData);

  // Execution viewer: opens as soon as the project is dispatching or executing.
  // The backend runs the combined dispatcher+execution stream as a background
  // job; this WS subscribes and replays/streams events. Disconnecting does NOT
  // stop the work, which is the whole point of the background model.
  const execution = useExecutionStream(
    projectId,
    project?.status === "dispatching" || project?.status === "executing",
    refreshProjectData,
  );

  const handleApprove = async () => {
    await decidePlan(projectId, true);
    await refreshProjectData();
  };

  const handleReject = async (feedback: string) => {
    await decidePlan(projectId, false, feedback);
    await refreshProjectData();
    // reject_plan on the backend seeds the user's feedback into the interview log
    // and flips status back to INTERVIEW, but doesn't run the Architect. Fire an
    // empty user_message through the existing WS — the server detects the seeded
    // message and drives the turn. Without this nudge, the user would have to
    // retype their feedback to get a response.
    if (wsStatus === "open") {
      send("");
    }
  };

  // The chat is "chattable" only in states where the Architect is the active listener.
  // The StatusBar explains what the system is doing at the top of the workspace, so
  // the chat's disabled message stays short — no duplication of info.
  const chattable =
    project?.status === "interview" ||
    project?.status === "planning" ||
    project?.status === "init";

  const chatDisabledReason = !chattable
    ? "Architect is on standby — see the status bar above."
    : wsStatus !== "open"
      ? "Connecting to server..."
      : undefined;

  // Center-panel tab state. Default: Plan during interview/planning/approval,
  // Tasks once dispatching begins. User can manually override with the tabs.
  const autoTab: "plan" | "tasks" =
    project &&
    (project.status === "dispatching" ||
      project.status === "executing" ||
      project.status === "phase_review" ||
      project.status === "complete" ||
      tasks.length > 0)
      ? "tasks"
      : "plan";
  const [centerTab, setCenterTab] = useState<"plan" | "tasks" | null>(null);
  const activeTab = centerTab ?? autoTab;

  // Right-column tab state. Starts on Decisions; auto-flips to Completed the first
  // time a task actually gets completed, so the user's eye lands on what just
  // happened. User can manually re-select either tab afterward.
  const [rightTab, setRightTab] = useState<"decisions" | "completed" | "agents">("decisions");
  const [autoFlippedToCompleted, setAutoFlippedToCompleted] = useState(false);
  const doneTaskCount = tasks.filter((t) => t.status === "done").length;
  useEffect(() => {
    if (doneTaskCount > 0 && !autoFlippedToCompleted) {
      setRightTab("completed");
      setAutoFlippedToCompleted(true);
    }
  }, [doneTaskCount, autoFlippedToCompleted]);

  // When a task is awaiting user review, find it so we can show the review panel.
  // There's at most one at a time — the execution loop halts on review.
  const taskInReview =
    project?.status === "awaiting_task_review"
      ? (tasks.find((t) => t.status === "review") ?? null)
      : null;

  // When project is blocked, surface the most recent block-related decision's
  // reason directly in the StatusBar so the user doesn't have to scroll the log.
  // Decisions are stored oldest-first; scan backward to find the most recent
  // *_blocked or dispatcher-related failure.
  const { blockedReason, isDispatcherBlock } = (() => {
    if (project?.status !== "blocked") {
      return { blockedReason: null as string | null, isDispatcherBlock: false };
    }
    for (let i = decisions.length - 1; i >= 0; i--) {
      const d = decisions[i];
      if (d.kind === "dispatcher_blocked") {
        return {
          blockedReason: (d as { reason?: string }).reason ?? "Dispatcher blocked.",
          isDispatcherBlock: true,
        };
      }
      if (d.kind === "task_blocked" || d.kind === "task_failed") {
        return {
          blockedReason:
            (d as { reason?: string }).reason ??
            "A task blocked. Check the Decisions log.",
          isDispatcherBlock: false,
        };
      }
    }
    return { blockedReason: null, isDispatcherBlock: false };
  })();

  const handleRetryDispatcher = async () => {
    setRetryingDispatcher(true);
    try {
      await retryDispatcher(projectId);
      // refreshProjectData picks up the new DISPATCHING status, which the
      // polling effect in activeStates will then track. The dispatcher
      // WebSocket hook reopens on status flip to DISPATCHING.
      await refreshProjectData();
    } catch (e) {
      console.error("Retry dispatcher failed", e);
    } finally {
      setRetryingDispatcher(false);
    }
  };

  const handleResumeExecution = async () => {
    setResumingExecution(true);
    try {
      await resumeExecution(projectId);
      // Status flips to EXECUTING; execution WS reopens on that flip.
      await refreshProjectData();
    } catch (e) {
      console.error("Resume execution failed", e);
    } finally {
      setResumingExecution(false);
    }
  };

  const handleAddWork = async () => {
    setAddingWork(true);
    try {
      await addWork(projectId);
      // Status flips to INTERVIEW; the Architect chat reopens with the
      // incremental-mode prompt that appends a new phase rather than rewriting.
      await refreshProjectData();
    } catch (e) {
      console.error("Add work failed", e);
    } finally {
      setAddingWork(false);
    }
  };

  const handlePause = async () => {
    setPausing(true);
    try {
      await pauseProject(projectId);
      // Status flips to PAUSED; execution loop halts at next task boundary.
      // The currently-running Coder or Reviewer (if any) finishes first.
      await refreshProjectData();
    } catch (e) {
      console.error("Pause failed", e);
    } finally {
      setPausing(false);
    }
  };

  const handleResumePaused = async () => {
    setResumingPaused(true);
    try {
      await resumePausedProject(projectId);
      // Status flips back to EXECUTING; the execution WS reopens and resumes.
      await refreshProjectData();
    } catch (e) {
      console.error("Resume failed", e);
    } finally {
      setResumingPaused(false);
    }
  };

  const [forceSubmittingPlan, setForceSubmittingPlan] = useState(false);
  const handleForceSubmitPlan = async () => {
    // Confirm — this bypasses the Architect, which is a nontrivial step. User
    // should understand what they're doing.
    const ok = window.confirm(
      "Force-submit plan for approval?\n\n" +
      "This bypasses the Architect and uses whatever's currently in plan.md " +
      "for approval. Only do this if the Architect is stuck logging 'handing off' " +
      "entries without actually calling request_approval.\n\n" +
      "If plan.md isn't ready yet, you can edit it directly in your editor " +
      "first (.devteam/plan.md in your project folder)."
    );
    if (!ok) return;
    setForceSubmittingPlan(true);
    try {
      const { forceSubmitPlan } = await import("../lib/api");
      await forceSubmitPlan(projectId);
      await refreshProjectData();
    } catch (e) {
      alert(`Force-submit failed: ${(e as Error).message}`);
    } finally {
      setForceSubmittingPlan(false);
    }
  };

  // Resume phases — for projects stuck at COMPLETE with unresolved phases in
  // plan.md (the multi-phase auto-advance bug). Confirm before kicking off
  // because the project will start running the Dispatcher again.
  const [resumingPhases, setResumingPhases] = useState(false);
  const handleResumePhases = async () => {
    const phases = project?.unresolved_phase_ids ?? [];
    const ok = window.confirm(
      `Resume work on ${phases.length} unfinished phase(s): ${phases.join(", ")}?\n\n` +
      "The Dispatcher will decompose the next undone phase into tasks, then the " +
      "execution loop will run them. Auto-advance to subsequent phases is now " +
      "fixed, so all remaining phases should run through to completion.\n\n" +
      "Use this for projects that completed before the multi-phase fix shipped — " +
      "they were marked done but P2/P3/etc. never actually ran."
    );
    if (!ok) return;
    setResumingPhases(true);
    try {
      const { resumePhases } = await import("../lib/api");
      await resumePhases(projectId);
      await refreshProjectData();
    } catch (e) {
      // 409 conflicts (project running, or pre-existing tasks for the phase)
      // include actionable detail in the message — show it verbatim so the
      // user knows what to investigate.
      alert(`Could not resume phases: ${(e as Error).message}`);
    } finally {
      setResumingPhases(false);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2 border-b border-line bg-panel/50">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onBack}
            className="text-[17px] text-gray-400 hover:text-gray-200"
          >
            ← Projects
          </button>
          <div className="text-[17px] font-semibold">{project?.name ?? "Loading..."}</div>
          {project && (
            <>
              <span className="text-[15px] text-gray-500">·</span>
              <span className="text-[15px] text-gray-500 font-mono">{project.root_path}</span>
            </>
          )}
        </div>
        {project && (() => {
          const totalInput =
            (project.tokens_input_opus ?? 0) +
            (project.tokens_input_sonnet ?? 0) +
            (project.tokens_input_haiku ?? 0);
          const totalCacheRead =
            (project.cache_read_opus ?? 0) +
            (project.cache_read_sonnet ?? 0) +
            (project.cache_read_haiku ?? 0);
          const cachePct = totalInput > 0 ? Math.round((totalCacheRead / totalInput) * 100) : 0;
          const tooltip =
            cachePct > 0
              ? `Estimated API cost. ${cachePct}% of input served from cache (billed at 10% of base rate).`
              : "Estimated API cost based on Anthropic public pricing. Not an actual bill.";
          return (
            <div className="text-[15px] text-gray-500 font-mono flex items-center gap-3">
              <span>
                {project.tokens_used.toLocaleString()} / {project.project_token_budget.toLocaleString()}{" "}
                tokens
              </span>
              {typeof project.cost_usd_estimate === "number" &&
                project.cost_usd_estimate > 0 && (
                  <span
                    className={
                      project.cost_usd_estimate >= 1
                        ? "text-amber-400"
                        : "text-gray-400"
                    }
                    title={tooltip}
                  >
                    ≈ ${project.cost_usd_estimate.toFixed(project.cost_usd_estimate < 1 ? 3 : 2)}
                  </span>
                )}
            </div>
          );
        })()}
      </div>

      {/* Task review panel — shown prominently when user must verify a UI task. */}
      {taskInReview && (
        <TaskReviewPanel
          projectId={projectId}
          task={taskInReview}
          onReviewed={refreshProjectData}
        />
      )}

      <StatusBar
        project={project}
        agentStreaming={
          streaming ||
          dispatcher.status === "running" ||
          execution.status === "running"
        }
        // The actual agent in motion. Execution-loop activity (Coder/Reviewer)
        // takes priority over Architect because the loop being live means real
        // work is happening; Architect streaming during execution is usually
        // just an idle background socket. Dispatcher in the middle when it's
        // decomposing. Falls back to "architect" only when nothing else is.
        activeAgent={
          execution.status === "running"
            ? execution.currentTask?.task_id?.toLowerCase().includes("review")
              ? "Reviewer"
              : "Coder"
            : dispatcher.status === "running"
              ? "Dispatcher"
              : streaming
                ? "Architect"
                : null
        }
        agentCurrentActivity={
          execution.status === "running" && execution.currentTask
            ? `${execution.currentTask.task_id}${
                execution.currentActivity ? ` · ${execution.currentActivity}` : ""
              }`
            : dispatcher.status === "running" && dispatcher.toolActivity.length > 0
              ? `using ${dispatcher.toolActivity[dispatcher.toolActivity.length - 1].name}`
              : streaming && toolActivity.length > 0
                ? `using ${toolActivity[toolActivity.length - 1].name}`
                : streaming && partialText
                  ? "drafting response"
                  : null
        }
        blockedReason={blockedReason}
        onRetryDispatcher={isDispatcherBlock ? handleRetryDispatcher : undefined}
        retryingDispatcher={retryingDispatcher}
        onResumeExecution={!isDispatcherBlock && project?.status === "blocked" ? handleResumeExecution : undefined}
        resumingExecution={resumingExecution}
        onAddWork={project?.status === "complete" ? handleAddWork : undefined}
        addingWork={addingWork}
        onPause={handlePause}
        pausing={pausing}
        onResumePaused={handleResumePaused}
        resumingPaused={resumingPaused}
        onOpenSettings={() => setShowSettings(true)}
        onForceSubmitPlan={handleForceSubmitPlan}
        forceSubmittingPlan={forceSubmittingPlan}
        onResumePhases={handleResumePhases}
        resumingPhases={resumingPhases}
        onOpenIn={async (target) => {
          try {
            await openProjectIn(projectId, target);
          } catch (err) {
            alert(
              `Couldn't open in ${target}: ${(err as Error).message}\n\n` +
                (target === "vscode"
                  ? "If VS Code isn't installed, install it from code.visualstudio.com. " +
                    "If it is installed, run: Cmd/Ctrl+Shift+P → 'Shell Command: Install code command in PATH'."
                  : "")
            );
          }
        }}
      />

      {/* Resizable workspace. Three columns split by drag handles, plus an
          inner vertical split inside the left column when the live-execution
          panel is visible. Each PanelGroup has an autoSaveId keyed to the
          project ID so layouts persist per-project across sessions.

          Min sizes prevent accidental collapse to nothing — you can squeeze
          a column down to ~10% of width but no further. The handles
          themselves are 1px lines with a wider hit area on hover; matches
          the existing divide-x aesthetic but draggable. */}
      <PanelGroup
        direction="horizontal"
        autoSaveId={`workspace:${projectId}`}
        className="flex-1 min-h-0"
      >
        {/* Left column: Architect chat. The Live Execution panel was removed
            because the Agents column on the right already streams the same
            tool-call activity per agent — having both was duplicative. The
            Agents column is now the single source of truth for live agent
            activity. */}
        <Panel defaultSize={40} minSize={15} className="flex flex-col min-h-0 min-w-0">
          <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
            <ArchitectChat
              interview={interview}
              streaming={streaming}
              partialText={partialText}
              toolActivity={toolActivity}
              tokensThisTurn={tokensThisTurn}
              disabled={!chattable || wsStatus !== "open"}
              disabledReason={chatDisabledReason}
              onSend={send}
              error={error}
            />
          </div>
        </Panel>

        <PanelResizeHandle className="w-px bg-line hover:w-1 hover:bg-accent transition-all" />

        {/* Center column: plan / tasks tabs */}
        <Panel defaultSize={40} minSize={15} className="flex flex-col min-h-0 min-w-0">
          <CenterTabs
            activeTab={activeTab}
            onSelect={setCenterTab}
            taskCount={tasks.length}
          />
          <div className="flex-1 min-h-0 min-w-0">
            {activeTab === "plan" ? (
              <PlanViewer
                plan={plan}
                project={project}
                onApprove={handleApprove}
                onReject={handleReject}
              />
            ) : (
              <TasksView
                tasks={tasks}
                project={project}
                dispatcherRunning={dispatcher.status === "running"}
                dispatcherActivity={dispatcher.toolActivity}
                dispatcherError={dispatcher.error}
                onTasksChanged={refreshProjectData}
              />
            )}
          </div>
        </Panel>

        <PanelResizeHandle className="w-px bg-line hover:w-1 hover:bg-accent transition-all" />

        {/* Right column: agents / decisions / completed tabs */}
        <Panel defaultSize={20} minSize={10} className="flex flex-col min-h-0 min-w-0">
          <RightTabs
            activeTab={rightTab}
            onSelect={setRightTab}
            doneCount={doneTaskCount}
            decisionCount={decisions.length}
          />
          <div className="flex-1 min-h-0 min-w-0">
            {rightTab === "decisions" ? (
              <DecisionsLog decisions={decisions} />
            ) : rightTab === "completed" ? (
              <CompletedTasks tasks={tasks} decisions={decisions} />
            ) : (
              <AgentInspector projectId={projectId} />
            )}
          </div>
        </Panel>
      </PanelGroup>

      {showSettings && project && (
        <EditProjectModal
          project={{
            id: project.id,
            name: project.name,
            root_path: project.root_path,
            status: project.status,
            created_at: project.created_at,
            tokens_used: project.tokens_used,
            tasks_completed: project.tasks_completed,
            is_running: project.is_running,
          }}
          onClose={() => setShowSettings(false)}
          onSaved={() => {
            setShowSettings(false);
            refreshProjectData();
          }}
        />
      )}
    </div>
  );
}

function CenterTabs({
  activeTab,
  onSelect,
  taskCount,
}: {
  activeTab: "plan" | "tasks";
  onSelect: (tab: "plan" | "tasks" | null) => void;
  taskCount: number;
}) {
  const tabClass = (active: boolean) =>
    `px-3 py-1.5 text-[15px] font-medium border-b-2 transition-colors ${
      active
        ? "border-accent text-gray-100"
        : "border-transparent text-gray-500 hover:text-gray-300"
    }`;
  return (
    <div className="flex flex-wrap border-b border-line bg-panel/30 overflow-x-auto">
      <button
        type="button"
        onClick={() => onSelect("plan")}
        className={tabClass(activeTab === "plan")}
      >
        Plan
      </button>
      <button
        type="button"
        onClick={() => onSelect("tasks")}
        className={tabClass(activeTab === "tasks")}
      >
        Tasks{taskCount > 0 ? ` (${taskCount})` : ""}
      </button>
    </div>
  );
}

function RightTabs({
  activeTab,
  onSelect,
  doneCount,
  decisionCount,
}: {
  activeTab: "decisions" | "completed" | "agents";
  onSelect: (tab: "decisions" | "completed" | "agents") => void;
  doneCount: number;
  decisionCount: number;
}) {
  const tabClass = (active: boolean) =>
    `px-3 py-1.5 text-[15px] font-medium border-b-2 transition-colors ${
      active
        ? "border-accent text-gray-100"
        : "border-transparent text-gray-500 hover:text-gray-300"
    }`;
  return (
    <div className="flex flex-wrap border-b border-line bg-panel/30 overflow-x-auto">
      <button
        type="button"
        onClick={() => onSelect("agents")}
        className={tabClass(activeTab === "agents")}
        title="Live transcript per agent: Architect, Dispatcher, Coder, Reviewer."
      >
        Agents
      </button>
      <button
        type="button"
        onClick={() => onSelect("decisions")}
        className={tabClass(activeTab === "decisions")}
      >
        Decisions{decisionCount > 0 ? ` (${decisionCount})` : ""}
      </button>
      <button
        type="button"
        onClick={() => onSelect("completed")}
        className={tabClass(activeTab === "completed")}
      >
        Completed{doneCount > 0 ? ` (${doneCount})` : ""}
      </button>
    </div>
  );
}
