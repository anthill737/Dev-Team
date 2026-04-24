// useExecutionStream — WebSocket hook for the execution loop.
//
// Unlike the dispatcher (which runs one turn), the execution loop runs until the phase
// completes, blocks, or is paused. This hook opens when project.status === "executing"
// (or dispatching right before the transition) and stays open through many tasks.
//
// State exposed to the UI:
//   - status: idle | connecting | running | done | error
//   - currentTask: { id, iteration, phase } | null — the one being worked on right now
//   - currentActivity: string | null — what the Coder is doing right now (tool name or text)
//   - completedInRun: array of { task_id, outcome_kind, summary } — finished this run
//   - recentActivity: ring buffer of human-readable events for the live feed
//   - error: string | null

import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "../lib/types";

export interface TaskCompletionEvent {
  task_id: string;
  outcome_kind: string;
  summary: string;
  at: number;
}

export interface ActivityEntry {
  at: number;
  kind:
    | "task_start"
    | "tool_use"
    | "tool_result"
    | "task_outcome"
    | "task_needs_user_review"
    | "phase_complete"
    | "project_complete"
    | "task_blocked"
    | "task_escalated"
    | "deadlock"
    | "budget_exceeded"
    | "scheduler_decision"
    | "error"
    | "info";
  text: string;
  isError?: boolean;
}

export interface CurrentTask {
  task_id: string;
  iteration: number;
}

export interface ExecutionStreamState {
  status: "idle" | "connecting" | "running" | "done" | "error";
  currentTask: CurrentTask | null;
  currentActivity: string | null;
  completedInRun: TaskCompletionEvent[];
  recentActivity: ActivityEntry[];
  error: string | null;
}

const INITIAL: ExecutionStreamState = {
  status: "idle",
  currentTask: null,
  currentActivity: null,
  completedInRun: [],
  recentActivity: [],
  error: null,
};

// Cap the live activity feed; we keep a window so the UI doesn't grow unbounded.
const ACTIVITY_WINDOW = 50;

export function useExecutionStream(
  projectId: string | null,
  shouldRun: boolean,
  onComplete?: () => void,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<ExecutionStreamState>(INITIAL);

  useEffect(() => {
    if (!projectId || !shouldRun) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }
    if (wsRef.current) return;

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws/execution/${projectId}`;
    setState({ ...INITIAL, status: "connecting" });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setState((s) => ({ ...s, status: "running" }));
    };

    ws.onmessage = (ev) => {
      let msg: WsEvent;
      try {
        msg = JSON.parse(ev.data) as WsEvent;
      } catch {
        return;
      }
      setState((s) => reduce(s, msg));
    };

    ws.onclose = () => {
      setState((s) => (s.status === "running" ? { ...s, status: "done" } : s));
      wsRef.current = null;
      if (onComplete) onComplete();
    };

    ws.onerror = () => {
      setState((s) => ({
        ...s,
        status: "error",
        error: "WebSocket error during execution",
      }));
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, shouldRun]);

  return state;
}

function appendActivity(
  s: ExecutionStreamState,
  entry: ActivityEntry,
): ExecutionStreamState {
  const next = [...s.recentActivity, entry];
  // Keep only the most recent ACTIVITY_WINDOW entries
  const trimmed = next.length > ACTIVITY_WINDOW ? next.slice(-ACTIVITY_WINDOW) : next;
  return { ...s, recentActivity: trimmed };
}

function reduce(s: ExecutionStreamState, msg: WsEvent): ExecutionStreamState {
  const now = Date.now();
  switch (msg.type) {
    case "task_start":
      return appendActivity(
        {
          ...s,
          currentTask: { task_id: msg.task_id, iteration: msg.iteration },
          currentActivity: `Starting iteration ${msg.iteration}`,
        },
        {
          at: now,
          kind: "task_start",
          text: `Task ${msg.task_id} started (iteration ${msg.iteration})`,
        },
      );

    case "tool_use":
      return appendActivity(
        { ...s, currentActivity: `Using ${msg.name}` },
        {
          at: now,
          kind: "tool_use",
          text: `${msg.name}(${briefInput(msg.input)})`,
        },
      );

    case "tool_result":
      return appendActivity(
        { ...s },
        {
          at: now,
          kind: "tool_result",
          text: msg.is_error
            ? `${msg.name} → error: ${(msg.preview || "").slice(0, 80)}`
            : `${msg.name} → ok`,
          isError: msg.is_error,
        },
      );

    case "task_outcome": {
      const completion: TaskCompletionEvent = {
        task_id: s.currentTask?.task_id ?? "?",
        outcome_kind: msg.outcome_kind ?? "unknown",
        summary: msg.summary ?? "",
        at: now,
      };
      return appendActivity(
        {
          ...s,
          currentTask: null,
          currentActivity: null,
          completedInRun: [...s.completedInRun, completion],
        },
        {
          at: now,
          kind: "task_outcome",
          text: `${completion.task_id} → ${completion.outcome_kind}${
            completion.summary ? `: ${completion.summary}` : ""
          }`,
          isError:
            completion.outcome_kind === "blocked" ||
            completion.outcome_kind === "failed",
        },
      );
    }

    case "scheduler_decision":
      // Filter noise: only show decisions that tell the user something meaningful.
      if (msg.decision_kind === "run_task") return s;
      return appendActivity(s, {
        at: now,
        kind: "scheduler_decision",
        text: `Scheduler: ${msg.decision_kind} — ${msg.reason}`,
      });

    case "phase_complete":
      return appendActivity(
        { ...s, currentTask: null, currentActivity: null },
        { at: now, kind: "phase_complete", text: `Phase ${msg.phase} complete` },
      );

    case "project_complete":
      return appendActivity(
        { ...s, currentTask: null, currentActivity: null },
        { at: now, kind: "project_complete", text: "Project complete" },
      );

    case "task_blocked":
      return appendActivity(s, {
        at: now,
        kind: "task_blocked",
        text: `Task ${msg.task_id} blocked: ${msg.reason}`,
        isError: true,
      });

    case "task_needs_user_review":
      // Halt-the-phase handoff: the Coder is done with what it can verify; the
      // user needs to look at it. Clear current task (Coder is no longer working)
      // and surface it prominently in the feed.
      return appendActivity(
        { ...s, currentTask: null, currentActivity: null },
        {
          at: now,
          kind: "task_needs_user_review",
          text: `Task ${msg.task_id} handed off for your review — see panel at top`,
        },
      );

    case "task_escalated":
      return appendActivity(s, {
        at: now,
        kind: "task_escalated",
        text: `Task ${msg.task_id} escalated: ${msg.reason}`,
        isError: true,
      });

    case "deadlock":
      return appendActivity(s, {
        at: now,
        kind: "deadlock",
        text: `Deadlock: ${msg.reason}`,
        isError: true,
      });

    case "budget_exceeded":
      return appendActivity(s, {
        at: now,
        kind: "budget_exceeded",
        text: `Budget exceeded on task ${msg.task_id} (limit ${msg.budget})`,
        isError: true,
      });

    case "loop_paused":
      return appendActivity(s, {
        at: now,
        kind: "info",
        text: `Paused: ${msg.reason}`,
      });

    case "loop_exit":
      return appendActivity(s, {
        at: now,
        kind: "info",
        text: `Execution stopped: ${msg.reason}`,
      });

    case "loop_safety_halt":
      return appendActivity(s, {
        at: now,
        kind: "info",
        text: "Execution loop safety halt — maximum iterations reached",
        isError: true,
      });

    case "error":
      return appendActivity(
        { ...s, error: msg.message, status: "error" },
        { at: now, kind: "error", text: msg.message, isError: true },
      );

    case "turn_complete":
      return { ...s, status: "done", currentTask: null, currentActivity: null };

    // text_delta and usage flow through from the Coder's inner APIRunner; for now we
    // don't show streaming coder text (it's dense and not very useful to the user) —
    // the tool activity is more informative. Usage rolls up into project tokens_used
    // which polling reflects.
    case "text_delta":
    case "usage":
      return s;

    default:
      return s;
  }
}

// Format tool input briefly for the activity feed. Keeps common cases readable.
function briefInput(input: Record<string, unknown>): string {
  if ("path" in input && typeof input.path === "string") return input.path;
  if ("argv" in input && Array.isArray(input.argv)) {
    return (input.argv as unknown[]).slice(0, 3).join(" ");
  }
  if ("status" in input && typeof input.status === "string") {
    return input.status;
  }
  return "";
}
