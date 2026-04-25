import { useState } from "react";
import { updateTask } from "../lib/api";
import type { ProjectDetail, Task } from "../lib/types";
import type { ToolEvent } from "../hooks/useArchitectStream";

interface Props {
  tasks: Task[];
  project: ProjectDetail | null;
  dispatcherRunning: boolean;
  dispatcherActivity: ToolEvent[];
  dispatcherError: string | null;
  // Called after any task edit so the parent re-fetches tasks + project state.
  // Optional so existing parents that haven't wired it don't break; when
  // absent, edits still succeed but the UI won't refresh until the next poll.
  onTasksChanged?: () => void;
}

export function TasksView({
  tasks,
  project,
  dispatcherRunning,
  dispatcherActivity,
  dispatcherError,
  onTasksChanged,
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
        <h2 className="text-[17px] font-semibold">
          Tasks{currentPhase ? ` — ${currentPhase}` : ""}
        </h2>
        <p className="text-[15px] text-gray-500">
          tasks.json — decomposed by the Dispatcher
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {dispatcherRunning && tasks.length === 0 && (
          <DispatcherLiveStatus activity={dispatcherActivity} />
        )}

        {dispatcherError && (
          <div className="text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            Dispatcher error: {dispatcherError}
          </div>
        )}

        {phaseTasks.length === 0 && !dispatcherRunning && (
          <div className="text-[17px] text-gray-500 italic">
            {project?.status === "await_approval"
              ? "Tasks will appear here once you approve the plan."
              : project?.status === "dispatching"
                ? "Dispatcher is working — tasks incoming."
                : "No tasks yet."}
          </div>
        )}

        {phaseTasks.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            projectId={project?.id || null}
            onChanged={onTasksChanged}
          />
        ))}

        {otherTasks.length > 0 && (
          <details className="mt-4">
            <summary className="text-[15px] text-gray-500 cursor-pointer hover:text-gray-400">
              Show {otherTasks.length} task{otherTasks.length === 1 ? "" : "s"} from other phases
            </summary>
            <div className="mt-2 space-y-2 opacity-70">
              {otherTasks.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  projectId={project?.id || null}
                  onChanged={onTasksChanged}
                />
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
      <div className="flex items-center gap-2 text-[17px] text-amber-200 font-medium">
        <div className="relative h-2 w-2">
          <div className="absolute inset-0 rounded-full bg-amber-400 animate-ping opacity-70" />
          <div className="absolute inset-0 rounded-full bg-amber-400" />
        </div>
        Dispatcher is decomposing the phase
      </div>
      {lastToolUse && (
        <div className="mt-1.5 text-[15px] font-mono text-amber-300/80">
          → {lastToolUse.name}
        </div>
      )}
      <div className="mt-2 text-[15px] text-amber-200/60">
        Reading the plan, drafting tasks with acceptance criteria, validating the structure...
      </div>
    </div>
  );
}

function TaskCard({
  task,
  projectId,
  onChanged,
}: {
  task: Task;
  projectId: string | null;
  onChanged?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [budget, setBudget] = useState<string>(String(task.budget_tokens));
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When the task changes (refresh after edit), keep the budget field in sync
  // so users see the latest value next time they open the editor.
  const resetEditState = () => {
    setBudget(String(task.budget_tokens));
    setNote("");
    setError(null);
  };

  const submitEdit = async (options: { interrupt?: boolean } = {}) => {
    if (!projectId) return;
    setSubmitting(true);
    setError(null);
    try {
      const args: { budget_tokens?: number; add_note?: string; interrupt?: boolean } = {};
      const b = Number(budget);
      if (Number.isFinite(b) && b > 0 && b !== task.budget_tokens) {
        args.budget_tokens = b;
      }
      if (note.trim()) {
        args.add_note = note.trim();
      }
      // Only send interrupt when there's a note to go with it — the backend
      // ignores naked interrupts anyway, but no point sending the flag without
      // a reason.
      if (options.interrupt && args.add_note) {
        args.interrupt = true;
      }
      if (!args.budget_tokens && !args.add_note) {
        setEditing(false);
        setSubmitting(false);
        return;
      }
      await updateTask(projectId, task.id, args);
      onChanged?.();
      setEditing(false);
      setNote("");  // clear note after successful send; keep budget showing new value
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="bg-panel border border-line rounded p-3">
      <button
        type="button"
        onClick={() => setExpanded((x) => !x)}
        className="w-full text-left"
      >
        <div className="flex items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2 min-w-0 flex-1">
            <span className="text-[15px] font-mono text-gray-500 flex-shrink-0">{task.id}</span>
            <span className="text-[17px] font-medium truncate">{task.title}</span>
          </div>
          <StatusBadge status={task.status} />
        </div>
        {!expanded && task.description && (
          <div className="mt-1 text-[15px] text-gray-400 line-clamp-2">{task.description}</div>
        )}
      </button>

      {expanded && (
        <div className="mt-3 space-y-2 text-[15px]">
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

          {/* User notes — previously-added notes show here so the user can see
              what's already attached without opening the editor. Only shows
              entries prefixed "User note:" (same prefix the backend applies),
              not Coder-iteration notes. */}
          {(() => {
            const userNotes = (task.notes || []).filter((n) =>
              n.startsWith("User note:"),
            );
            if (userNotes.length === 0) return null;
            return (
              <div className="pt-2 border-t border-line/60">
                <div className="text-amber-500/80 mb-1 font-medium">
                  Your notes ({userNotes.length})
                </div>
                <ul className="space-y-1">
                  {userNotes.map((n, i) => (
                    <li
                      key={i}
                      className="text-amber-200/80 bg-amber-950/20 border border-amber-900/30 rounded px-2 py-1"
                    >
                      {n.replace(/^User note:\s*/, "")}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })()}

          {/* Edit controls — only for non-done tasks with a projectId available. */}
          {task.status !== "done" && projectId && (
            <div className="pt-2 border-t border-line/60">
              {!editing ? (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    resetEditState();
                    setEditing(true);
                  }}
                  className="text-[15px] text-gray-400 hover:text-gray-200 border border-line hover:border-gray-600 rounded px-2 py-1"
                  title="Edit this task's budget or add a note the Coder will see on next iteration."
                >
                  ⚙ Settings
                </button>
              ) : (
                <div className="space-y-2">
                  <div>
                    <label className="block text-gray-500 mb-0.5">Budget (tokens)</label>
                    <input
                      type="number"
                      min={1}
                      value={budget}
                      onChange={(e) => setBudget(e.target.value)}
                      className="w-full bg-ink border border-line rounded px-2 py-1 text-[15px] font-mono"
                    />
                  </div>
                  <div>
                    <label className="block text-gray-500 mb-0.5">
                      Append a note for the Coder (optional)
                    </label>
                    <textarea
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      rows={2}
                      placeholder="e.g. try using the react-query library"
                      className="w-full bg-ink border border-line rounded px-2 py-1 text-[15px]"
                    />
                  </div>
                  {error && (
                    <div className="text-red-400 bg-red-950/40 border border-red-900/50 rounded px-2 py-1">
                      {error}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2 justify-end">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditing(false);
                      }}
                      disabled={submitting}
                      className="text-[15px] text-gray-400 hover:text-gray-200 px-2 py-1 disabled:opacity-40"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        submitEdit({ interrupt: true });
                      }}
                      disabled={submitting || !note.trim()}
                      className="text-[15px] bg-red-800 hover:bg-red-700 text-white font-medium px-2 py-1 rounded disabled:opacity-40"
                      title={
                        !note.trim()
                          ? "Add a note first — interrupt needs a message."
                          : "Pauses execution and surfaces this task for you to respond. Needs a note."
                      }
                    >
                      {submitting ? "…" : "Save & interrupt"}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        submitEdit();
                      }}
                      disabled={submitting}
                      className="text-[15px] bg-accent hover:bg-amber-400 text-black font-medium px-2 py-1 rounded disabled:opacity-40"
                      title="Save the note/budget. The Coder picks it up on its next iteration — execution continues."
                    >
                      {submitting ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
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
    <span className={`text-[15px] px-2 py-0.5 rounded flex-shrink-0 ${color}`}>
      {status.replace("_", " ")}
    </span>
  );
}
