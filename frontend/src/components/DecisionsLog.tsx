import type { DecisionEntry } from "../lib/types";

interface Props {
  decisions: DecisionEntry[];
}

export function DecisionsLog({ decisions }: Props) {
  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-3 border-b border-line">
        <h2 className="text-sm font-semibold">Decisions</h2>
        <p className="text-xs text-gray-500">decisions.log — agent research, rationale, state changes</p>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-1 font-mono text-xs">
        {decisions.length === 0 ? (
          <div className="text-gray-500 italic font-sans">
            Nothing logged yet. Research findings and key decisions will appear here as the
            Architect works.
          </div>
        ) : (
          decisions
            .slice()
            .reverse()
            .map((d, i) => <DecisionRow key={i} entry={d} />)
        )}
      </div>
    </div>
  );
}

function DecisionRow({ entry }: { entry: DecisionEntry }) {
  const time = new Date(entry.timestamp * 1000).toLocaleTimeString();
  const actor = entry.actor ?? "system";
  const kind = entry.kind ?? "";

  let body: string;
  if (entry.note) {
    body = String(entry.note);
  } else if (entry.query) {
    body = `"${entry.query}"${entry.result_count !== undefined ? ` → ${entry.result_count} results` : ""}`;
  } else if (entry.url) {
    body = String(entry.url);
  } else if (entry.feedback) {
    body = String(entry.feedback);
  } else if (entry.summary) {
    body = String(entry.summary);
  } else if (entry.to) {
    body = `→ ${entry.to}`;
  } else {
    body = "";
  }

  const actorColor =
    actor === "architect"
      ? "text-amber-400"
      : actor === "user"
        ? "text-blue-400"
        : actor === "orchestrator"
          ? "text-gray-400"
          : "text-gray-300";

  return (
    <div className="py-1 border-b border-line/60">
      <div className="flex gap-2 items-baseline">
        <span className="text-gray-600">{time}</span>
        <span className={actorColor}>{actor}</span>
        <span className="text-gray-500">·</span>
        <span className="text-gray-300">{kind}</span>
      </div>
      {body && <div className="mt-0.5 text-gray-400 break-words">{body}</div>}
    </div>
  );
}
