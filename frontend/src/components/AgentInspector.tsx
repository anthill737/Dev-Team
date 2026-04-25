// AgentInspector — unified per-agent transcript viewer.
//
// Five tabs (Architect, Dispatcher, Coder, Reviewer, All) over a single
// transcript panel. Polls the backend's in-memory agent event buffer at
// 1.5s intervals using `since=<highest seq seen>` so each request is cheap.
//
// Renders three event categories:
//   - Text deltas (model output) → coalesced into a streaming paragraph
//   - Tool calls → expandable card showing tool name, input, and (later) result
//   - Status events (turn_complete, task_start, etc.) → faint divider/heading
//
// Auto-scrolls to the bottom on new events unless the user has scrolled up
// (lets you read older content without it jumping). Activity dot on each
// tab indicates events received in the last 5 seconds.

import { useEffect, useMemo, useRef, useState } from "react";
import { getAgentEvents, getAgentSummary } from "../lib/api";
import type { AgentEvent, AgentRole, AgentSummaryEntry } from "../lib/api";

interface Props {
  projectId: string;
  // Polling rate in ms. Default 1500 for events, 1000 for summary.
  pollIntervalMs?: number;
}

// Tabs to show. "all" is a synthetic tab that shows merged events from all
// agents in chronological order — useful for debugging or seeing the full
// flow of a complex task.
const TAB_AGENTS: (AgentRole | "all")[] = [
  "architect",
  "dispatcher",
  "coder",
  "reviewer",
  "all",
];

const TAB_LABELS: Record<AgentRole | "all", string> = {
  architect: "Architect",
  dispatcher: "Dispatcher",
  coder: "Coder",
  reviewer: "Reviewer",
  orchestrator: "Orchestrator",
  all: "All",
};

// An agent is "active" if it produced an event in the last 5 seconds. Used
// for the pulsing dot on the tab.
const ACTIVE_THRESHOLD_MS = 5000;

export function AgentInspector({ projectId, pollIntervalMs = 1500 }: Props) {
  const [selected, setSelected] = useState<AgentRole | "all">("all");
  // Per-agent event buffer in the frontend. Keyed by agent role.
  // The "all" tab is rendered by merging across roles client-side.
  const [eventsByAgent, setEventsByAgent] = useState<
    Record<AgentRole, AgentEvent[]>
  >({
    architect: [],
    dispatcher: [],
    coder: [],
    reviewer: [],
    orchestrator: [],
  });
  // Highest seq we've seen across all agents — used as the `since` cursor
  // for the next poll. Single counter is fine because the backend assigns
  // seq monotonically per project across all agents.
  const [latestSeq, setLatestSeq] = useState(0);
  const [summary, setSummary] = useState<
    Record<AgentRole, AgentSummaryEntry>
  >({
    architect: empty(),
    dispatcher: empty(),
    coder: empty(),
    reviewer: empty(),
    orchestrator: empty(),
  });

  const transcriptRef = useRef<HTMLDivElement | null>(null);
  // Whether the user has scrolled away from the bottom. We disable auto-scroll
  // while they're reading older content; resume when they scroll back to
  // bottom. Trigger threshold: within 50px of the actual bottom.
  const [autoScroll, setAutoScroll] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Reset state when project changes — otherwise tabs would show stale events
  // from the previous project.
  useEffect(() => {
    setEventsByAgent({
      architect: [],
      dispatcher: [],
      coder: [],
      reviewer: [],
      orchestrator: [],
    });
    setLatestSeq(0);
    setSummary({
      architect: empty(),
      dispatcher: empty(),
      coder: empty(),
      reviewer: empty(),
      orchestrator: empty(),
    });
    setError(null);
  }, [projectId]);

  // Poll for new events. We keep a ref to `latestSeq` so the polling closure
  // always reads the current value without restarting on every event. Each
  // request uses `since=<latestSeq>` so the response is the tail since last
  // poll. We chain via setTimeout so a slow response doesn't queue overlap.
  const latestSeqRef = useRef(latestSeq);
  useEffect(() => {
    latestSeqRef.current = latestSeq;
  }, [latestSeq]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const tick = async () => {
      if (cancelled) return;
      try {
        const since = latestSeqRef.current;
        const resp = await getAgentEvents(projectId, undefined, since);
        if (cancelled) return;
        if (resp.events.length > 0) {
          // Group new events by agent then append. A single event always
          // belongs to one agent (server-side attribution); we don't mirror.
          const grouped: Record<AgentRole, AgentEvent[]> = {
            architect: [],
            dispatcher: [],
            coder: [],
            reviewer: [],
            orchestrator: [],
          };
          for (const e of resp.events) {
            grouped[e.agent]?.push(e);
          }
          setEventsByAgent((prev) => ({
            architect: [...prev.architect, ...grouped.architect],
            dispatcher: [...prev.dispatcher, ...grouped.dispatcher],
            coder: [...prev.coder, ...grouped.coder],
            reviewer: [...prev.reviewer, ...grouped.reviewer],
            orchestrator: [...prev.orchestrator, ...grouped.orchestrator],
          }));
        }
        setLatestSeq(resp.latest_seq);
        setError(null);
      } catch (e) {
        // Don't spam the UI on a transient backend hiccup; surface and retry.
        setError((e as Error).message);
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(tick, pollIntervalMs);
        }
      }
    };

    timer = window.setTimeout(tick, 100);

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [projectId, pollIntervalMs]);

  // Poll the summary endpoint on a slightly tighter cadence (1s) so the
  // tab indicators feel responsive even when the events panel is between
  // 1.5s polls. Cheap call (no event payload data).
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const tick = async () => {
      if (cancelled) return;
      try {
        const resp = await getAgentSummary(projectId);
        if (cancelled) return;
        // Strip orchestrator from the tab summary view — it's noisy and not
        // a tab the user clicks. Still tracked internally for the "all" view.
        setSummary(resp.agents);
      } catch {
        // Ignore summary errors entirely; events poll will surface real issues.
      } finally {
        if (!cancelled) timer = window.setTimeout(tick, 1000);
      }
    };
    timer = window.setTimeout(tick, 0);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [projectId]);

  // Auto-scroll to bottom on new events when user is at/near the bottom.
  // Tracks scroll position to detect when the user has scrolled up to read
  // older content; in that case, leave the scroll position alone.
  const visibleEvents = useMemo(() => {
    if (selected === "all") {
      const merged: AgentEvent[] = [];
      (Object.keys(eventsByAgent) as AgentRole[]).forEach((role) => {
        merged.push(...eventsByAgent[role]);
      });
      merged.sort((a, b) => a.seq - b.seq);
      return merged;
    }
    return eventsByAgent[selected];
  }, [selected, eventsByAgent]);

  useEffect(() => {
    if (autoScroll && transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [visibleEvents.length, autoScroll]);

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    // Within 50px of the bottom = "at bottom", auto-scroll re-engages.
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setAutoScroll(atBottom);
  };

  const now = Date.now();

  return (
    <div className="border border-line rounded-lg bg-panel/30 overflow-hidden flex flex-col h-full">
      {/* Tab strip — flex-wrap so the 5 agent tabs reflow into 2 rows when
          the column is narrow (which is the default in the right slot of
          the workspace grid). overflow-x-auto is a fallback for unusual
          widths where wrapping doesn't trigger. */}
      <div className="flex flex-wrap items-center border-b border-line bg-panel/50 overflow-x-auto">
        {TAB_AGENTS.map((tab) => {
          const isSelected = selected === tab;
          // For "all" tab we don't have a single summary; sum across agents.
          const tabSummary =
            tab === "all"
              ? null
              : (summary[tab as AgentRole] as AgentSummaryEntry | undefined);
          const eventCount =
            tab === "all"
              ? Object.values(eventsByAgent).reduce(
                  (acc, arr) => acc + arr.length,
                  0,
                )
              : (tabSummary?.event_count ?? 0);
          // "Active" = produced an event recently. For the all tab, active if
          // any agent is active.
          const isActive =
            tab === "all"
              ? Object.values(summary).some(
                  (s) => s.last_ts > 0 && now - s.last_ts * 1000 < ACTIVE_THRESHOLD_MS,
                )
              : tabSummary !== undefined &&
                tabSummary !== null &&
                tabSummary.last_ts > 0 &&
                now - tabSummary.last_ts * 1000 < ACTIVE_THRESHOLD_MS;

          return (
            <button
              key={tab}
              type="button"
              onClick={() => {
                setSelected(tab);
                setAutoScroll(true);
              }}
              className={`flex items-center gap-1.5 px-3 py-2 text-[15px] font-medium transition-colors border-b-2 ${
                isSelected
                  ? "border-emerald-500 text-gray-100 bg-panel/30"
                  : "border-transparent text-gray-400 hover:text-gray-200"
              }`}
            >
              {isActive && (
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
              )}
              <span>{TAB_LABELS[tab]}</span>
              {eventCount > 0 && (
                <span className="text-[13px] text-gray-500">
                  ({eventCount})
                </span>
              )}
            </button>
          );
        })}
      </div>

      {error && (
        <div className="px-3 py-1 text-[13px] text-amber-400 bg-amber-950/20 border-b border-amber-900/40">
          Polling error: {error} (will retry)
        </div>
      )}

      {/* Transcript */}
      <div
        ref={transcriptRef}
        onScroll={handleScroll}
        className="flex-1 min-w-0 overflow-y-auto overflow-x-hidden px-3 py-2 text-[15px] font-mono"
      >
        {visibleEvents.length === 0 ? (
          <div className="text-gray-500 text-[15px] italic">
            {selected === "all"
              ? "No agent activity yet. Events will appear here as agents run."
              : `No ${TAB_LABELS[selected]} activity yet.`}
          </div>
        ) : (
          <TranscriptView events={visibleEvents} showAgentBadge={selected === "all"} />
        )}
      </div>

      {!autoScroll && (
        <div className="px-3 py-1 border-t border-line bg-panel/30 flex items-center justify-between">
          <span className="text-[13px] text-gray-500">
            Auto-scroll paused (scrolled up)
          </span>
          <button
            type="button"
            onClick={() => {
              setAutoScroll(true);
              if (transcriptRef.current) {
                transcriptRef.current.scrollTop =
                  transcriptRef.current.scrollHeight;
              }
            }}
            className="text-[13px] text-emerald-400 hover:text-emerald-300"
          >
            Jump to bottom ↓
          </button>
        </div>
      )}
    </div>
  );
}

// --- Transcript renderer ----------------------------------------------------
//
// Coalesces consecutive text_delta events from the same agent into one
// paragraph. Renders tool_use_start as an expandable card. Renders tool_result
// inline below its preceding tool call when seqs match. Other event kinds
// render as faint dividers.

function TranscriptView({
  events,
  showAgentBadge,
}: {
  events: AgentEvent[];
  showAgentBadge: boolean;
}) {
  // Build a render plan. We walk events in order, collapsing consecutive
  // text_deltas from the same agent into a single text block, and pairing
  // tool_use_start with the matching tool_result.
  type Block =
    | { kind: "text"; agent: AgentRole; text: string; firstSeq: number }
    | {
        kind: "tool";
        agent: AgentRole;
        seq: number;
        name: string;
        input: Record<string, unknown>;
        result: { content: unknown; isError: boolean } | null;
        taskId: string | null;
      }
    | {
        kind: "status";
        agent: AgentRole;
        seq: number;
        kindLabel: string;
        payload: Record<string, unknown>;
      };

  const blocks: Block[] = [];
  // Map of tool_use_id → block index, so when a tool_result arrives later we
  // can find its matching tool_use_start and attach the result.
  const toolBlockByUseId: Record<string, number> = {};

  for (const e of events) {
    if (e.kind === "text_delta") {
      const text = String(e.payload.text ?? "");
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "text" && last.agent === e.agent) {
        last.text += text;
      } else {
        blocks.push({
          kind: "text",
          agent: e.agent,
          text,
          firstSeq: e.seq,
        });
      }
    } else if (e.kind === "tool_use_start") {
      const toolUseId = String(e.payload.id ?? `seq-${e.seq}`);
      const blockIdx = blocks.length;
      blocks.push({
        kind: "tool",
        agent: e.agent,
        seq: e.seq,
        name: String(e.payload.name ?? "?"),
        input: (e.payload.input as Record<string, unknown>) ?? {},
        result: null,
        taskId: e.task_id,
      });
      toolBlockByUseId[toolUseId] = blockIdx;
    } else if (e.kind === "tool_result") {
      const toolUseId = String(e.payload.tool_use_id ?? "");
      const idx = toolBlockByUseId[toolUseId];
      if (idx !== undefined && blocks[idx]?.kind === "tool") {
        (blocks[idx] as Extract<Block, { kind: "tool" }>).result = {
          content: e.payload.content,
          isError: Boolean(e.payload.is_error),
        };
      }
      // If we don't have a matching tool call (lost the start event somehow),
      // render the result as a status block so it's not silently dropped.
      else {
        blocks.push({
          kind: "status",
          agent: e.agent,
          seq: e.seq,
          kindLabel: "tool_result (orphaned)",
          payload: e.payload,
        });
      }
    } else {
      // turn_complete, task_start, usage, error, etc.
      blocks.push({
        kind: "status",
        agent: e.agent,
        seq: e.seq,
        kindLabel: e.kind,
        payload: e.payload,
      });
    }
  }

  return (
    <div className="space-y-2">
      {blocks.map((b, i) => (
        <BlockView key={i} block={b} showAgentBadge={showAgentBadge} />
      ))}
    </div>
  );
}

function BlockView({
  block,
  showAgentBadge,
}: {
  block:
    | { kind: "text"; agent: AgentRole; text: string; firstSeq: number }
    | {
        kind: "tool";
        agent: AgentRole;
        seq: number;
        name: string;
        input: Record<string, unknown>;
        result: { content: unknown; isError: boolean } | null;
        taskId: string | null;
      }
    | {
        kind: "status";
        agent: AgentRole;
        seq: number;
        kindLabel: string;
        payload: Record<string, unknown>;
      };
  showAgentBadge: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  if (block.kind === "text") {
    return (
      // break-words handles the common case; overflow-wrap-anywhere catches
      // truly unbreakable tokens (long URLs, snake_case identifiers, paths).
      // min-w-0 lets flex/grid parents constrain width despite child content.
      <div
        className="text-gray-200 whitespace-pre-wrap break-words leading-relaxed min-w-0"
        style={{ overflowWrap: "anywhere" }}
      >
        {showAgentBadge && <AgentBadge agent={block.agent} />}
        {block.text}
      </div>
    );
  }

  if (block.kind === "tool") {
    const inputStr = JSON.stringify(block.input, null, 2);
    const inputPreview =
      inputStr.length > 200 ? inputStr.slice(0, 200) + "…" : inputStr;
    return (
      <div className="border border-line/60 rounded bg-black/30 overflow-hidden">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center justify-between gap-2 px-2 py-1 hover:bg-black/40 text-left"
        >
          <div className="flex items-center gap-2 min-w-0">
            {showAgentBadge && <AgentBadge agent={block.agent} />}
            <span className="text-cyan-400 shrink-0">⚙</span>
            <span className="text-cyan-300 font-semibold truncate">
              {block.name}
            </span>
            {block.taskId && (
              <span className="text-[13px] text-gray-500 shrink-0">
                {block.taskId}
              </span>
            )}
          </div>
          <span className="text-gray-500 text-[13px]">
            {expanded ? "▾" : "▸"}
          </span>
        </button>
        {!expanded && (
          <div className="px-2 pb-1 text-[13px] text-gray-500 truncate">
            {inputPreview}
          </div>
        )}
        {expanded && (
          <div className="px-2 pb-2 space-y-1.5">
            <div>
              <div className="text-[13px] uppercase tracking-wider text-gray-500 mt-1">
                Input
              </div>
              <pre className="text-[13px] text-gray-300 bg-black/40 rounded px-2 py-1 overflow-x-auto">
                {inputStr}
              </pre>
            </div>
            {block.result && (
              <div>
                <div className="text-[13px] uppercase tracking-wider text-gray-500">
                  Result {block.result.isError && "(error)"}
                </div>
                <pre
                  className={`text-[13px] rounded px-2 py-1 overflow-x-auto whitespace-pre-wrap ${
                    block.result.isError
                      ? "text-red-300 bg-red-950/30"
                      : "text-gray-300 bg-black/40"
                  }`}
                >
                  {formatToolResult(block.result.content)}
                </pre>
              </div>
            )}
            {!block.result && (
              <div className="text-[13px] text-gray-500 italic">
                Waiting for result…
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // status block — faint divider with kind label
  return (
    <div className="text-[13px] text-gray-500 border-t border-line/30 pt-1">
      {showAgentBadge && <AgentBadge agent={block.agent} />}
      <span className="uppercase tracking-wider">{block.kindLabel}</span>
      {Object.keys(block.payload).length > 0 && (
        <span className="ml-2 text-gray-600">
          {summarizeStatusPayload(block.kindLabel, block.payload)}
        </span>
      )}
    </div>
  );
}

function AgentBadge({ agent }: { agent: AgentRole }) {
  const colorMap: Record<AgentRole, string> = {
    architect: "bg-purple-900/40 text-purple-200",
    dispatcher: "bg-blue-900/40 text-blue-200",
    coder: "bg-emerald-900/40 text-emerald-200",
    reviewer: "bg-amber-900/40 text-amber-200",
    orchestrator: "bg-gray-800 text-gray-400",
  };
  return (
    <span
      className={`inline-block px-1.5 py-0.5 rounded text-xs uppercase tracking-wider mr-1.5 ${colorMap[agent]}`}
    >
      {agent}
    </span>
  );
}

function formatToolResult(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((c) => {
        if (typeof c === "object" && c !== null && "text" in c) {
          return String((c as { text: unknown }).text);
        }
        return JSON.stringify(c);
      })
      .join("\n");
  }
  return JSON.stringify(content, null, 2);
}

function summarizeStatusPayload(
  kind: string,
  payload: Record<string, unknown>,
): string {
  // Pull a small, human-readable summary out of the payload based on kind.
  // Keeps the status divider line compact even for verbose payloads.
  const meaningful = ["task_id", "stop_reason", "phase", "reason"];
  const parts: string[] = [];
  for (const key of meaningful) {
    if (key in payload) {
      parts.push(`${key}=${String(payload[key])}`);
    }
  }
  if (kind === "usage") {
    const inT = payload.input_tokens;
    const outT = payload.output_tokens;
    if (typeof inT === "number" || typeof outT === "number") {
      parts.push(`in=${inT ?? 0} out=${outT ?? 0}`);
    }
  }
  return parts.join(" · ");
}

function empty(): AgentSummaryEntry {
  return { event_count: 0, last_seq: 0, last_ts: 0, last_kind: null };
}
