// CompletedTasks — dedicated dashboard panel showing tasks the dev team has finished.
//
// Grouping: by phase (P1, P2, ...), with each phase expanded by default. Within a phase,
// tasks appear in completion order (newest at top) so the user sees "what just happened"
// first. Each row shows task id, title, summary from the Coder, and iteration count.

import { useMemo } from "react";
import type { DecisionEntry, Task } from "../lib/types";

interface Props {
  tasks: Task[];
  decisions: DecisionEntry[];
}

interface CompletedTaskRow {
  task: Task;
  summary: string;
  completedAt: number | null;
}

export function CompletedTasks({ tasks, decisions }: Props) {
  const byPhase = useMemo(() => buildRows(tasks, decisions), [tasks, decisions]);
  const phases = Object.keys(byPhase).sort();
  const totalDone = tasks.filter((t) => t.status === "done").length;

  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-3 border-b border-line">
        <h2 className="text-sm font-semibold">Completed tasks</h2>
        <p className="text-xs text-gray-500">
          {totalDone === 0
            ? "No tasks completed yet."
            : `${totalDone} task${totalDone === 1 ? "" : "s"} done across ${phases.length} phase${phases.length === 1 ? "" : "s"}.`}
        </p>
      </div>

      <div className="flex-1 overflow-y-auto">
        {phases.length === 0 && (
          <div className="p-4 text-xs text-gray-500 italic">
            Tasks the dev team completes will appear here, grouped by phase, with summaries
            of what was built.
          </div>
        )}
        {phases.map((phase) => (
          <PhaseGroup key={phase} phase={phase} rows={byPhase[phase]} />
        ))}
      </div>
    </div>
  );
}

function PhaseGroup({ phase, rows }: { phase: string; rows: CompletedTaskRow[] }) {
  // Newest first within phase
  const ordered = [...rows].sort((a, b) => {
    const aAt = a.completedAt ?? 0;
    const bAt = b.completedAt ?? 0;
    return bAt - aAt;
  });
  return (
    <div className="border-b border-line">
      <div className="px-4 py-2 bg-panel/40 sticky top-0 z-10">
        <div className="text-xs font-semibold text-gray-200">
          {phase} <span className="text-gray-500 font-normal">({rows.length})</span>
        </div>
      </div>
      <div>
        {ordered.map((row) => (
          <TaskRow key={row.task.id} row={row} />
        ))}
      </div>
    </div>
  );
}

function TaskRow({ row }: { row: CompletedTaskRow }) {
  const { task, summary, completedAt } = row;
  const time = completedAt ? new Date(completedAt * 1000).toLocaleTimeString() : "";
  const iterationsLabel =
    task.iterations > 1 ? ` · ${task.iterations} iterations` : "";
  return (
    <div className="px-4 py-2 border-b border-line/50 last:border-b-0">
      <div className="flex items-baseline gap-2">
        <span className="text-xs font-mono text-emerald-500">{task.id}</span>
        <span className="text-xs text-gray-200 truncate">{task.title}</span>
      </div>
      {summary && (
        <div className="mt-1 text-xs text-gray-400 break-words">{summary}</div>
      )}
      <div className="mt-1 text-[10px] text-gray-500 font-mono">
        {time}
        {iterationsLabel}
      </div>
    </div>
  );
}

function buildRows(
  tasks: Task[],
  decisions: DecisionEntry[],
): Record<string, CompletedTaskRow[]> {
  // Primary source: fields stamped directly onto the task when it was approved.
  // Fallback: older tasks (completed before the summary field existed) may need
  // to pull their summary from the decisions log if it's still in range.
  const fallbackByTaskId: Record<string, { summary: string; at: number }> = {};
  for (const d of decisions) {
    if (d.kind === "task_approved" && typeof d.task_id === "string") {
      const summary = typeof d.summary === "string" ? d.summary : "";
      const ts = typeof d.timestamp === "number" ? d.timestamp : undefined;
      fallbackByTaskId[d.task_id] = {
        summary,
        at: ts ?? Date.now() / 1000,
      };
    }
  }

  const out: Record<string, CompletedTaskRow[]> = {};
  for (const task of tasks) {
    if (task.status !== "done") continue;

    // Prefer the task's own fields; fall back to decisions for legacy rows.
    const fallback = fallbackByTaskId[task.id];
    const summary = task.summary ?? fallback?.summary ?? "";
    const completedAt = task.completed_at ?? fallback?.at ?? null;

    const row: CompletedTaskRow = { task, summary, completedAt };
    if (!out[task.phase]) out[task.phase] = [];
    out[task.phase].push(row);
  }
  return out;
}
