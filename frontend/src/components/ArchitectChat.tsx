import { memo, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { InterviewTurn } from "../lib/types";
import type { ToolEvent } from "../hooks/useArchitectStream";

interface Props {
  interview: InterviewTurn[];
  streaming: boolean;
  partialText: string;
  toolActivity: ToolEvent[];
  tokensThisTurn: { input: number; output: number };
  disabled: boolean;
  disabledReason?: string;
  onSend: (text: string) => void;
  error: string | null;
}

export function ArchitectChat({
  interview,
  streaming,
  partialText,
  toolActivity,
  tokensThisTurn,
  disabled,
  disabledReason,
  onSend,
  error,
}: Props) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Auto-scroll to bottom on new content
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [interview.length, partialText, toolActivity.length]);

  const canSend = input.trim().length > 0 && !streaming && !disabled;

  const send = () => {
    if (!canSend) return;
    onSend(input.trim());
    setInput("");
  };

  return (
    <div className="h-full flex flex-col">
      {(tokensThisTurn.input > 0 || tokensThisTurn.output > 0) && (
        // Token counter only — when there's nothing to count, we render no
        // header at all so the column is just chat. The previous header
        // ("Architect / Senior engineer, interviewing you") was misleading
        // once execution started: the Architect is on standby for most of
        // the project's lifecycle, and the status bar already names the
        // active agent. Removed to free vertical space for chat content.
        <div className="px-4 py-2 border-b border-line flex items-baseline justify-end">
          <div className="text-[13px] text-gray-500 font-mono">
            {tokensThisTurn.input.toLocaleString()}↓ {tokensThisTurn.output.toLocaleString()}↑
          </div>
        </div>
      )}

      <div ref={scrollRef} className="flex-1 min-w-0 overflow-y-auto overflow-x-hidden p-4 space-y-4">
        {interview.length === 0 && !streaming && (
          <div className="text-[17px] text-gray-500 italic">
            The Architect will interview you to understand what you want to build. Start by
            describing your project idea — the more context you give, the better.
          </div>
        )}

        {interview.map((turn, i) => (
          <MessageBubble key={i} role={turn.role} content={turn.content} />
        ))}

        {toolActivity.length > 0 && <ToolActivityPanel events={toolActivity} />}

        {streaming && partialText && <MessageBubble role="assistant" content={partialText} streaming />}

        {streaming && !partialText && toolActivity.length === 0 && (
          <div className="text-[17px] text-gray-500 italic">Architect is thinking...</div>
        )}

        {error && (
          <div className="text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            {error}
          </div>
        )}
      </div>

      <div className="p-3 border-t border-line">
        {disabled && disabledReason && (
          <div className="text-[15px] text-gray-500 mb-2">{disabledReason}</div>
        )}
        <div className="flex gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={disabled ? "" : "Describe your project, or answer the Architect's question..."}
            disabled={disabled}
            rows={3}
            className="flex-1 bg-ink border border-line rounded px-3 py-2 text-[17px] resize-none focus:outline-none focus:border-accent disabled:opacity-50"
          />
          <button
            type="button"
            disabled={!canSend}
            onClick={send}
            className="self-stretch bg-accent text-black font-medium px-4 rounded disabled:opacity-40 hover:bg-amber-400"
          >
            Send
          </button>
        </div>
        <p className="text-[15px] text-gray-600 mt-2">Cmd+Enter to send</p>
      </div>
    </div>
  );
}

const MessageBubble = memo(function MessageBubble({
  role,
  content,
  streaming,
}: {
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
}) {
  const isUser = role === "user";
  return (
    // The outer flex container must have min-w-0 so it honors max-width on
    // the inner bubble. Without it, flex children can grow beyond constraints.
    <div className={`flex min-w-0 ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] min-w-0 rounded-lg px-3 py-2 text-[17px] prose-chat overflow-hidden ${
          isUser
            ? "bg-amber-900/30 border border-amber-900/40"
            : "bg-panel border border-line"
        }`}
      >
        {isUser ? (
          <div className="whitespace-pre-wrap break-words">{content}</div>
        ) : streaming ? (
          // While streaming, render as plain text — parsing markdown on every
          // text_delta is O(n²) (the whole growing string gets re-parsed) and
          // is the #1 source of jitter. Markdown styling kicks in on turn_complete.
          <>
            <div className="whitespace-pre-wrap break-words">{content}</div>
            <span className="inline-block w-1.5 h-4 bg-accent ml-0.5 align-middle animate-pulse" />
          </>
        ) : (
          <ReactMarkdown>{content}</ReactMarkdown>
        )}
      </div>
    </div>
  );
});

function ToolActivityPanel({ events }: { events: ToolEvent[] }) {
  return (
    <div className="bg-ink/60 border border-line rounded p-2 text-[15px] font-mono space-y-1">
      {events.map((ev, i) => (
        <div key={i} className={ev.isError ? "text-red-400" : "text-gray-400"}>
          <span className="text-gray-600">{ev.kind === "use" ? "→" : "←"}</span>{" "}
          <span className="text-accent">{ev.name}</span>{" "}
          {ev.kind === "use" && ev.input && (
            <span className="text-gray-500">{truncate(JSON.stringify(ev.input), 80)}</span>
          )}
          {ev.kind === "result" && ev.preview && (
            <span className="text-gray-500">{truncate(ev.preview, 80)}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n) + "…";
}
