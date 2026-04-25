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
              {isActive ? (
                <span
                  className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse"
                  title="Active in the last 5 seconds"
                />
              ) : eventCount > 0 ? (
                // Gray dot = this agent has run during the project but is
                // currently idle. Helps you see at a glance which agents
                // have any history at all (Reviewer "no dot" tells you it
                // has never run for this project).
                <span
                  className="h-1.5 w-1.5 rounded-full bg-gray-600"
                  title="Has events but currently idle"
                />
              ) : null}
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
  // ============================================================
  // STAGE 1: Group events by (task_id, iteration).
  //
  // Why: a flat stream of "fs_read fs_read fs_read" cards is unreadable.
  // The Coder works in iterations on tasks. Grouping events into per-iteration
  // cards makes the structure of the work visible — you can see "P2-T6
  // iteration 3 had 12 tool calls and ended with the Reviewer rejecting" as
  // a visual unit, then collapse it and look at iteration 4.
  //
  // Heuristic: the orchestrator emits `task_start` events with task_id +
  // iteration. Events between two task_starts (or with the same task_id)
  // belong to one iteration. Events without a task_id (Architect chat,
  // Dispatcher decomposition, orchestrator status events) go into a special
  // "ambient" group at the top so they don't get lost.
  // ============================================================
  type Group = {
    key: string; // unique group id for React
    taskId: string | null; // null = ambient group
    iteration: number | null;
    agent: AgentRole | null; // dominant agent for this group
    events: AgentEvent[];
  };

  const groups: Group[] = [];
  let currentGroup: Group | null = null;
  let ambientGroup: Group | null = null;

  for (const e of events) {
    // task_start kicks off a new task/iteration group, even if the previous
    // group had the same task_id (e.g. iteration N+1).
    if (e.kind === "task_start") {
      const taskId = String(e.payload.task_id ?? e.task_id ?? "?");
      const iteration =
        typeof e.payload.iteration === "number" ? e.payload.iteration : null;
      currentGroup = {
        key: `${taskId}-iter${iteration ?? "?"}-seq${e.seq}`,
        taskId,
        iteration,
        agent: null,
        events: [e],
      };
      groups.push(currentGroup);
      continue;
    }

    // Events tagged with a task_id continue the current group if it matches,
    // or start a new task group when the task changes.
    if (e.task_id) {
      if (!currentGroup || currentGroup.taskId !== e.task_id) {
        currentGroup = {
          key: `${e.task_id}-orphan-seq${e.seq}`,
          taskId: e.task_id,
          iteration: null,
          agent: null,
          events: [],
        };
        groups.push(currentGroup);
      }
      currentGroup.events.push(e);
      // Track the dominant agent — the one that owns the most events in this
      // group. Used to color the group header.
      if (e.agent !== "orchestrator") {
        currentGroup.agent = e.agent;
      }
      continue;
    }

    // No task_id → ambient (Architect interview, Dispatcher decomposition,
    // orchestrator-level status events). Pin to a single ambient group at
    // the top of the panel so user can read the conversation flow without
    // it bleeding into the per-task transcripts.
    if (!ambientGroup) {
      ambientGroup = {
        key: "ambient",
        taskId: null,
        iteration: null,
        agent: null,
        events: [],
      };
      groups.unshift(ambientGroup);
    }
    ambientGroup.events.push(e);
    if (e.agent !== "orchestrator" && !ambientGroup.agent) {
      ambientGroup.agent = e.agent;
    }
  }

  // ============================================================
  // STAGE 2: Render each group as a collapsible card.
  //
  // Most-recent group expanded; everything older collapsed. User can click
  // headers to toggle. Ambient group always expanded since it's the running
  // conversation.
  // ============================================================
  if (groups.length === 0) {
    return (
      <div className="text-[15px] text-gray-500 italic">
        Nothing yet — agent activity will appear here.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {groups.map((g, idx) => {
        const isLast = idx === groups.length - 1;
        const isAmbient = g.taskId === null;
        return (
          <GroupCard
            key={g.key}
            group={g}
            showAgentBadge={showAgentBadge}
            defaultExpanded={isAmbient || isLast}
          />
        );
      })}
    </div>
  );
}

// Renders one task/iteration group as a collapsible card with a header
// showing the task, agent, iteration, and event-count summary. Body is
// the existing flat block list, unchanged.
function GroupCard({
  group,
  showAgentBadge,
  defaultExpanded,
}: {
  group: {
    taskId: string | null;
    iteration: number | null;
    agent: AgentRole | null;
    events: AgentEvent[];
  };
  showAgentBadge: boolean;
  defaultExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  // Update expansion when defaultExpanded flips (e.g. a new group becomes the
  // most-recent one and should auto-expand). Keeps user's manual toggles
  // intact for older groups.
  const prevDefaultRef = useRef(defaultExpanded);
  useEffect(() => {
    if (defaultExpanded && !prevDefaultRef.current) {
      setExpanded(true);
    }
    prevDefaultRef.current = defaultExpanded;
  }, [defaultExpanded]);

  // Quick stats for the header — total events, tool calls, whether a
  // submit_review showed up. Cheap, computed on each render; this list
  // is bounded by _MAX_EVENTS_PER_AGENT.
  const stats = useMemo(() => {
    let toolCalls = 0;
    let textChunks = 0;
    let reviewOutcome: "approve" | "request_changes" | null = null;
    for (const e of group.events) {
      if (e.kind === "tool_use_start") toolCalls += 1;
      else if (e.kind === "text_delta") textChunks += 1;
      // submit_review's outcome lives on the tool input
      if (e.kind === "tool_use_start" && e.payload.name === "submit_review") {
        const outcome = (e.payload.input as Record<string, unknown> | undefined)
          ?.outcome;
        if (outcome === "approve" || outcome === "request_changes") {
          reviewOutcome = outcome;
        }
      }
    }
    return { toolCalls, textChunks, reviewOutcome };
  }, [group.events]);

  // Header label. Ambient = just an "Activity" header. Task groups show the
  // task ID, iteration, and agent.
  const headerLabel =
    group.taskId === null
      ? "Conversation"
      : `${group.taskId}${
          group.iteration !== null ? ` · iteration ${group.iteration}` : ""
        }${group.agent ? ` · ${group.agent}` : ""}`;

  return (
    // Border around each group makes the boundaries visible without being
    // heavy. Negative space + borders, no full background fills.
    <div className="border border-line/40 rounded overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-panel/30 text-left bg-panel/20"
      >
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-gray-500 shrink-0 text-[13px]">
            {expanded ? "▾" : "▸"}
          </span>
          <span className="text-gray-200 font-medium truncate">
            {headerLabel}
          </span>
          {stats.reviewOutcome === "approve" && (
            <span className="text-[12px] px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-300 shrink-0">
              approved
            </span>
          )}
          {stats.reviewOutcome === "request_changes" && (
            <span className="text-[12px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 shrink-0">
              rework
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-[13px] text-gray-500 shrink-0">
          {stats.textChunks > 0 && <span>{stats.textChunks} chunks</span>}
          {stats.toolCalls > 0 && <span>{stats.toolCalls} tools</span>}
        </div>
      </button>
      {expanded && (
        <div className="p-2 space-y-2">
          <FlatBlocks events={group.events} showAgentBadge={showAgentBadge} />
        </div>
      )}
    </div>
  );
}

// The original flat-block rendering from the old TranscriptView, extracted
// so GroupCard can reuse it for each group's body.
function FlatBlocks({
  events,
  showAgentBadge,
}: {
  events: AgentEvent[];
  showAgentBadge: boolean;
}) {
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
      } else {
        blocks.push({
          kind: "status",
          agent: e.agent,
          seq: e.seq,
          kindLabel: "tool_result (orphaned)",
          payload: e.payload,
        });
      }
    } else if (e.kind === "task_start") {
      // task_start is consumed by the group header; don't render it as a
      // status row inside the body or it duplicates the header.
      continue;
    } else if (
      e.kind === "usage" ||
      e.kind === "turn_complete" ||
      e.kind === "tool_use_complete"
    ) {
      // Low-signal observability events. `usage` is per-call token accounting
      // that floods the panel between every interesting event. `turn_complete`
      // and `tool_use_complete` are end-markers — the next event already shows
      // that the previous one finished. Hiding these by default cuts roughly
      // half the row count without losing user-relevant information. Token
      // totals are visible in the status bar; per-call usage doesn't need to
      // be in the inspector body.
      continue;
    } else {
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
    // For the one-line collapsed view, extract the most informative single
    // value from the tool input. For most of our tools that's a path,
    // filename, or first argv. Falls back to a compact JSON snippet.
    const singleLinePreview = (() => {
      const inp = block.input;
      if (typeof inp.path === "string") return inp.path;
      if (typeof inp.filename === "string") return inp.filename;
      if (Array.isArray(inp.argv) && inp.argv.length > 0) {
        return inp.argv.slice(0, 3).join(" ") + (inp.argv.length > 3 ? " …" : "");
      }
      if (typeof inp.query === "string") {
        return inp.query.length > 60 ? inp.query.slice(0, 60) + "…" : inp.query;
      }
      // Fallback: compact JSON, truncated
      const compact = JSON.stringify(inp);
      return compact.length > 80 ? compact.slice(0, 80) + "…" : compact;
    })();

    return (
      // No outer border in collapsed state — the row is dense by design;
      // borders on every tool call would create visual stripes. The expand
      // affordance is the chevron + hover background.
      <div className="rounded overflow-hidden">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-2 px-2 py-0.5 hover:bg-black/30 text-left"
        >
          <span className="text-gray-500 shrink-0 text-[13px] w-3">
            {expanded ? "▾" : "▸"}
          </span>
          {showAgentBadge && <AgentBadge agent={block.agent} />}
          <span className="text-cyan-300 font-semibold shrink-0 text-[14px]">
            {block.name}
          </span>
          <span
            className="text-[13px] text-gray-400 truncate min-w-0"
            style={{ overflowWrap: "anywhere" }}
          >
            {singleLinePreview}
          </span>
          {block.result?.isError && (
            <span className="text-[12px] text-red-400 shrink-0">error</span>
          )}
        </button>
        {expanded && (
          <div className="px-3 pb-2 pt-1 space-y-1.5 border-l-2 border-line/40 ml-3">
            <div>
              <div className="text-[12px] uppercase tracking-wider text-gray-500">
                Input
              </div>
              <pre className="text-[13px] text-gray-300 bg-black/40 rounded px-2 py-1 overflow-x-auto">
                {inputStr}
              </pre>
            </div>
            {block.result && (
              <div>
                <div className="text-[12px] uppercase tracking-wider text-gray-500">
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
