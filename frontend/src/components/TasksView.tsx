import { useState } from "react";
import type { ProjectDetail, Task } from "../lib/types";
import type { ToolEvent } from "../hooks/useArchitectStream";

interface Props {
  tasks: Task[];
  project: ProjectDetail | null;
  dispatcherRunning: boolean;
  dispatcherActivity: ToolEvent[];
  dispatcherError: string | null;
}

export function TasksView({
  tasks,
  project,
  dispatcherRunning,
  dispatcherActivity,
  dispatcherError,
}: Props) {
  const currentPhase = project?.current_phase;
  const phaseTasks = currentPhase
    ? tasks.filter((t) => t.phase === currentPhase)
    : tasks;
  const otherTasks = currentPhase
    ? tasks.filter((t) => t.phase !== currentPhase)
    : [];

  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-3 border-b border-line">
        <h2 className="text-sm font-semibold">
          Tasks{currentPhase ? ` — ${currentPhase}` : ""}
        </h2>
        <p className="text-xs text-gray-500">
          tasks.json — decomposed by the Dispatcher
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {dispatcherRunning && tasks.length === 0 && (
          <DispatcherLiveStatus activity={dispatcherActivity} />
        )}

        {dispatcherError && (
          <div className="text-sm text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            Dispatcher error: {dispatcherError}
          </div>
        )}

        {phaseTasks.length === 0 && !dispatcherRunning && (
          <div className="text-sm text-gray-500 italic">
            {project?.status === "await_approval"
              ? "Tasks will appear here once you approve the plan."
              : project?.status === "dispatching"
                ? "Dispatcher is working — tasks incoming."
                : "No tasks yet."}
          </div>
        )}

        {phaseTasks.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}

        {otherTasks.length > 0 && (
          <details className="mt-4">
            <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-400">
              Show {otherTasks.length} task{otherTasks.length === 1 ? "" : "s"} from other phases
            </summary>
            <div className="mt-2 space-y-2 opacity-70">
              {otherTasks.map((task) => (
                <TaskCard key={task.id} task={task} />
              ))}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

function DispatcherLiveStatus({ activity }: { activity: ToolEvent[] }) {
  const lastToolUse = [...activity].reverse().find((a) => a.kind === "use");
  return (
    <div className="bg-amber-950/30 border border-amber-800/50 rounded p-3">
      <div className="flex items-center gap-2 text-sm text-amber-200 font-medium">
        <div className="relative h-2 w-2">
          <div className="absolute inset-0 rounded-full bg-amber-400 animate-ping opacity-70" />
          <div className="absolute inset-0 rounded-full bg-amber-400" />
        </div>
        Dispatcher is decomposing the phase
      </div>
      {lastToolUse && (
        <div className="mt-1.5 text-xs font-mono text-amber-300/80">
          → {lastToolUse.name}
        </div>
      )}
      <div className="mt-2 text-xs text-amber-200/60">
        Reading the plan, drafting tasks with acceptance criteria, validating the structure...
      </div>
    </div>
  );
}

function TaskCard({ task }: { task: Task }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="bg-panel border border-line rounded p-3">
      <button
        type="button"
        onClick={() => setExpanded((x) => !x)}
        className="w-full text-left"
      >
        <div className="flex items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2 min-w-0 flex-1">
            <span className="text-xs font-mono text-gray-500 flex-shrink-0">{task.id}</span>
            <span className="text-sm font-medium truncate">{task.title}</span>
          </div>
          <StatusBadge status={task.status} />
        </div>
        {!expanded && task.description && (
          <div className="mt-1 text-xs text-gray-400 line-clamp-2">{task.description}</div>
        )}
      </button>

      {expanded && (
        <div className="mt-3 space-y-2 text-xs">
          <div>
            <div className="text-gray-500 mb-1">Description</div>
            <div className="text-gray-300 whitespace-pre-wrap">{task.description}</div>
          </div>
          <div>
            <div className="text-gray-500 mb-1">
              Acceptance criteria ({task.acceptance_criteria.length})
            </div>
            <ul className="space-y-1">
              {task.acceptance_criteria.map((ac, i) => (
                <li key={i} className="text-gray-300 flex gap-2">
                  <span className="text-gray-600">✓</span>
                  <span>{ac}</span>
                </li>
              ))}
            </ul>
          </div>
          {task.dependencies.length > 0 && (
            <div>
              <div className="text-gray-500 mb-1">Dependencies</div>
              <div className="flex gap-1 flex-wrap">
                {task.dependencies.map((d) => (
                  <span
                    key={d}
                    className="font-mono text-gray-400 bg-ink px-1.5 py-0.5 rounded"
                  >
                    {d}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div className="flex gap-4 text-gray-500 pt-1 border-t border-line/60">
            <span>Assigned: {task.assigned_to}</span>
            <span>Budget: {task.budget_tokens.toLocaleString()} tokens</span>
            {task.iterations > 0 && <span>Iterations: {task.iterations}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: Task["status"] }) {
  const color =
    status === "done"
      ? "bg-green-900/40 text-green-300"
      : status === "blocked"
        ? "bg-red-900/40 text-red-300"
        : status === "in_progress"
          ? "bg-amber-900/40 text-amber-300"
          : status === "review"
            ? "bg-blue-900/40 text-blue-300"
            : "bg-gray-800 text-gray-400";
  return (
    <span className={`text-xs px-2 py-0.5 rounded flex-shrink-0 ${color}`}>
      {status.replace("_", " ")}
    </span>
  );
}
