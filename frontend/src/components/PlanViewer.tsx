import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { ProjectDetail } from "../lib/types";

interface Props {
  plan: string;
  project: ProjectDetail | null;
  onApprove: () => Promise<void>;
  onReject: (feedback: string) => Promise<void>;
}

export function PlanViewer({ plan, project, onApprove, onReject }: Props) {
  const [busy, setBusy] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState<string | null>(null);

  const awaitingApproval = project?.status === "await_approval";

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      await onApprove();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    if (!feedback.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onReject(feedback.trim());
      setShowReject(false);
      setFeedback("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-3 border-b border-line flex items-baseline justify-between">
        <div>
          <h2 className="text-sm font-semibold">Plan</h2>
          <p className="text-xs text-gray-500">plan.md — updated by the Architect</p>
        </div>
        {awaitingApproval && (
          <span className="text-xs px-2 py-0.5 rounded bg-amber-900/40 text-amber-300">
            awaiting approval
          </span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {plan.trim() ? (
          <div className="prose-chat text-sm max-w-none">
            <ReactMarkdown>{plan}</ReactMarkdown>
          </div>
        ) : (
          <div className="text-sm text-gray-500 italic">
            No plan yet. The Architect will write <code className="bg-ink px-1">plan.md</code>{" "}
            once the interview is thorough enough.
          </div>
        )}
      </div>

      {awaitingApproval && (
        <div className="border-t border-line p-3">
          {error && (
            <div className="mb-2 text-xs text-red-400 bg-red-950/40 border border-red-900/50 rounded px-2 py-1">
              {error}
            </div>
          )}
          {showReject ? (
            <div className="space-y-2">
              <textarea
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder="What should the Architect revise? Be specific."
                rows={3}
                className="w-full bg-ink border border-line rounded px-3 py-2 text-sm resize-none focus:outline-none focus:border-accent"
              />
              <div className="flex gap-2 justify-end">
                <button
                  type="button"
                  onClick={() => {
                    setShowReject(false);
                    setFeedback("");
                  }}
                  className="text-sm px-3 py-1.5 rounded hover:bg-ink"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={busy || !feedback.trim()}
                  onClick={reject}
                  className="text-sm bg-panel border border-line px-3 py-1.5 rounded disabled:opacity-40 hover:border-gray-600"
                >
                  Send feedback
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => setShowReject(true)}
                className="flex-1 text-sm bg-panel border border-line py-2 rounded disabled:opacity-40 hover:border-gray-600"
              >
                Request changes
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={approve}
                className="flex-1 text-sm bg-accent text-black font-medium py-2 rounded disabled:opacity-40 hover:bg-amber-400"
              >
                {busy ? "..." : "Approve plan"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
