// LiveExecution — renders the live state of the execution loop during a phase run.
//
// Shows:
//   - Current task (id + iteration) if one is in progress
//   - Current activity (the tool the Coder is using right now)
//   - Scrolling activity feed of recent events (tool calls, outcomes, blocks)
//
// When nothing's running, shows a "waiting" state. This component should always be
// rendered in the UI while the project is in an execution-capable state so the user
// can glance and know exactly what's happening.

import { useEffect, useRef } from "react";
import type { ActivityEntry, ExecutionStreamState } from "../hooks/useExecutionStream";

interface Props {
  stream: ExecutionStreamState;
}

export function LiveExecution({ stream }: Props) {
  const feedRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the bottom as new activity arrives
  useEffect(() => {
    if (feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight;
    }
  }, [stream.recentActivity.length]);

  return (
    <div className="border border-line rounded-lg bg-panel/30 overflow-hidden flex flex-col max-h-full">
      <div className="px-3 py-2 border-b border-line flex items-center justify-between">
        <div>
          <div className="text-xs font-semibold text-gray-200">Live execution</div>
          <div className="text-[10px] text-gray-500">
            {stream.status === "running"
              ? stream.currentTask
                ? `Coder working on ${stream.currentTask.task_id} · iteration ${stream.currentTask.iteration}`
                : "Between tasks…"
              : stream.status === "connecting"
                ? "Connecting…"
                : stream.status === "done"
                  ? "Run complete"
                  : stream.status === "error"
                    ? "Error"
                    : "Idle"}
          </div>
        </div>
        {stream.status === "running" && (
          <div className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
        )}
      </div>

      {/* Current activity — the most prominent piece; updates in real time */}
      {stream.currentActivity && (
        <div className="px-3 py-2 border-b border-line/50 bg-panel/40">
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-0.5">
            Right now
          </div>
          <div className="text-xs text-gray-200 font-mono truncate">
            {stream.currentActivity}
          </div>
        </div>
      )}

      {/* Completed tasks in this run */}
      {stream.completedInRun.length > 0 && (
        <div className="px-3 py-2 border-b border-line/50">
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            Completed this run
          </div>
          <div className="space-y-0.5">
            {stream.completedInRun.slice(-5).map((c, i) => (
              <div key={i} className="flex items-baseline gap-2 text-xs">
                <span
                  className={`font-mono ${outcomeColor(c.outcome_kind)}`}
                  title={c.outcome_kind}
                >
                  {c.task_id}
                </span>
                <span className="text-gray-500">{outcomeLabel(c.outcome_kind)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Activity feed — scrolling log */}
      <div
        ref={feedRef}
        className="flex-1 overflow-y-auto p-2 space-y-0.5 font-mono text-[11px] min-h-[120px]"
      >
        {stream.recentActivity.length === 0 && (
          <div className="text-gray-600 italic text-center py-4">
            Activity will appear here as the dev team works.
          </div>
        )}
        {stream.recentActivity.map((a, i) => (
          <ActivityRow key={i} entry={a} />
        ))}
      </div>

      {stream.error && (
        <div className="px-3 py-2 border-t border-red-900 bg-red-950/40 text-xs text-red-300">
          {stream.error}
        </div>
      )}
    </div>
  );
}

function ActivityRow({ entry }: { entry: ActivityEntry }) {
  const time = new Date(entry.at).toLocaleTimeString();
  const colorClass = entry.isError
    ? "text-red-400"
    : entry.kind === "task_outcome" || entry.kind === "review_approved"
      ? "text-emerald-400"
      : entry.kind === "phase_complete" || entry.kind === "project_complete"
        ? "text-emerald-300 font-semibold"
        : entry.kind === "task_start"
          ? "text-blue-300"
          : entry.kind === "review_start"
            ? "text-amber-300"
            : "text-gray-400";

  return (
    <div className="flex gap-2 items-baseline">
      <span className="text-gray-600 shrink-0">{time}</span>
      <span className={`${colorClass} break-words`}>{entry.text}</span>
    </div>
  );
}

function outcomeColor(kind: string): string {
  switch (kind) {
    case "approved":
      return "text-emerald-400";
    case "needs_rework":
      return "text-amber-400";
    case "needs_user_review":
      return "text-amber-300";
    case "blocked":
    case "failed":
      return "text-red-400";
    default:
      return "text-gray-400";
  }
}

function outcomeLabel(kind: string): string {
  switch (kind) {
    case "approved":
      return "done";
    case "needs_rework":
      return "needs rework";
    case "needs_user_review":
      return "awaiting your review";
    case "blocked":
      return "blocked";
    case "failed":
      return "failed";
    default:
      return kind;
  }
}
