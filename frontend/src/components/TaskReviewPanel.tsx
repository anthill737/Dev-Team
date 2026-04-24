// TaskReviewPanel — shown at the top of the workspace when a task needs user review.
//
// The Coder finished a UI/visual task, can't verify it from bash, and has handed off
// to the user. This panel is the user's instructions + controls:
//
//   - What was built (summary from the Coder)
//   - Exact command to run/view it (one click to copy)
//   - Checklist of things to verify (interactive — user can tick off as they check)
//   - Files to look at (for context)
//   - Approve button (task done, execution continues)
//   - Reject button (requires feedback, task goes back to Coder)
//
// Must be visible and actionable. If the user doesn't see this, the whole team is
// blocked and nothing happens.

import { useState } from "react";
import { reviewTask } from "../lib/api";
import type { Task } from "../lib/types";

interface Props {
  projectId: string;
  task: Task;
  onReviewed: () => void;
}

export function TaskReviewPanel({ projectId, task, onReviewed }: Props) {
  const [checkedItems, setCheckedItems] = useState<Set<number>>(new Set());
  const [showRejectForm, setShowRejectForm] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const checklist = task.review_checklist ?? [];
  const runCommand = task.review_run_command ?? "";
  const filesToCheck = task.review_files_to_check ?? [];
  const summary = task.review_summary ?? "";

  // Interrupt mode: the user hit "Save & interrupt" on the task, which halted
  // execution and landed here. Coder-review fields (checklist, run command,
  // summary) are irrelevant — there's no Coder work to verify. What IS
  // relevant is the user's own note and the options to resume or send
  // feedback. Rendered via a distinct branch so we don't try to cram both
  // cases into one layout.
  const isInterrupt = task.interrupted_by_user === true;
  const userNotes = (task.notes || []).filter((n) => n.startsWith("User note:"));
  // The latest user note is the one that triggered the interrupt — most
  // relevant to surface at the top.
  const latestNote = userNotes.length > 0
    ? userNotes[userNotes.length - 1].replace(/^User note:\s*/, "")
    : "";

  const allChecked =
    checklist.length > 0 && checkedItems.size === checklist.length;

  const toggleItem = (index: number) => {
    setCheckedItems((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const copyRunCommand = async () => {
    try {
      await navigator.clipboard.writeText(runCommand);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Couldn't copy to clipboard — please select and copy manually.");
    }
  };

  const handleApprove = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await reviewTask(projectId, task.id, true);
      onReviewed();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approval failed");
    } finally {
      setSubmitting(false);
    }
  };

  const handleReject = async () => {
    if (!feedback.trim()) {
      setError("Please tell the Coder what needs to change.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await reviewTask(projectId, task.id, false, feedback.trim());
      onReviewed();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Rejection failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (isInterrupt) {
    // Interrupt panel: user hit "Save & interrupt" on a task. Show their note
    // front and center. Two actions:
    //   - Resume (replaces Approve): sends approved=true; backend resets task
    //     to pending and resumes the Coder with the note visible in next
    //     read_task. Task is NOT marked done — it wasn't actually finished.
    //   - Send changes (replaces Needs changes): same reject flow, user's
    //     feedback goes into task.notes and Coder retries.
    return (
      <div className="border-b-2 border-red-600/70 bg-red-950/20">
        <div className="px-4 py-3 flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2 mb-2">
              <span className="text-[10px] uppercase tracking-wider font-semibold text-red-300">
                Execution paused — you interrupted this task
              </span>
              <span className="text-xs font-mono text-gray-400">{task.id}</span>
              <span className="text-xs text-gray-200 truncate">{task.title}</span>
            </div>

            {latestNote && (
              <div className="mb-3">
                <div className="text-[10px] uppercase tracking-wider text-amber-400/80 mb-1">
                  Your note
                </div>
                <div className="text-sm text-amber-100 bg-amber-950/40 border border-amber-900/50 rounded px-3 py-2 whitespace-pre-wrap">
                  {latestNote}
                </div>
              </div>
            )}

            <div className="text-xs text-gray-400 mb-2">
              Resume to send the Coder back to this task (your note is visible
              in the task's notes — the Coder will see it on its next iteration).
              Or send additional feedback to guide the rework.
            </div>

            {showRejectForm && (
              <div className="mt-3 p-2 border border-line rounded bg-black/30">
                <label
                  htmlFor="interrupt-feedback"
                  className="block text-[10px] uppercase tracking-wider text-gray-500 mb-1"
                >
                  Additional feedback for the Coder
                </label>
                <textarea
                  id="interrupt-feedback"
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  placeholder="e.g., Actually, also make sure X, and avoid approach Y."
                  rows={3}
                  className="w-full text-xs bg-black/50 border border-line rounded px-2 py-1.5 text-gray-200 font-mono"
                  disabled={submitting}
                />
              </div>
            )}

            {error && <div className="mt-2 text-xs text-red-400">{error}</div>}
          </div>

          {/* Action buttons — different labels for interrupt context */}
          <div className="flex flex-col gap-1.5 shrink-0 w-36">
            {!showRejectForm ? (
              <>
                <button
                  type="button"
                  onClick={handleApprove}
                  disabled={submitting}
                  className="px-3 py-2 text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white rounded disabled:opacity-50"
                  title="Send the Coder back to this task. Your note is in the task's notes; the Coder will see it on its next iteration."
                >
                  {submitting ? "..." : "Resume Coder"}
                </button>
                <button
                  type="button"
                  onClick={() => setShowRejectForm(true)}
                  disabled={submitting}
                  className="px-3 py-2 text-xs bg-amber-800/80 hover:bg-amber-700/90 text-amber-100 rounded disabled:opacity-50"
                  title="Add more feedback before the Coder retries."
                >
                  Add more feedback
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={handleReject}
                  disabled={submitting || !feedback.trim()}
                  className="px-3 py-2 text-xs font-semibold bg-amber-700 hover:bg-amber-600 text-white rounded disabled:opacity-50"
                >
                  {submitting ? "..." : "Send feedback"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowRejectForm(false);
                    setFeedback("");
                  }}
                  disabled={submitting}
                  className="px-3 py-2 text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 rounded disabled:opacity-50"
                >
                  Cancel
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="border-b-2 border-amber-600/70 bg-amber-950/20">
      <div className="px-4 py-3 flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 mb-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-amber-400">
              Needs your review
            </span>
            <span className="text-xs font-mono text-gray-400">{task.id}</span>
            <span className="text-xs text-gray-200 truncate">{task.title}</span>
          </div>
          {summary && (
            <div className="text-xs text-gray-300 mb-3">{summary}</div>
          )}

          <div className="grid grid-cols-[1fr_1fr] gap-4 mt-2">
            {/* Left: Run command + checklist */}
            <div>
              {runCommand && (
                <div className="mb-3">
                  <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">
                    How to run it
                  </div>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 text-xs font-mono bg-black/40 text-emerald-300 px-2 py-1.5 rounded break-all">
                      {runCommand}
                    </code>
                    <button
                      type="button"
                      onClick={copyRunCommand}
                      className="text-xs px-2 py-1 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded shrink-0"
                      title="Copy to clipboard"
                    >
                      {copied ? "Copied!" : "Copy"}
                    </button>
                  </div>
                </div>
              )}

              {checklist.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">
                    Check this ({checkedItems.size}/{checklist.length})
                  </div>
                  <ul className="space-y-1">
                    {checklist.map((item, i) => (
                      <li key={i} className="flex items-start gap-2">
                        <input
                          type="checkbox"
                          id={`check-${i}`}
                          checked={checkedItems.has(i)}
                          onChange={() => toggleItem(i)}
                          className="mt-0.5"
                        />
                        <label
                          htmlFor={`check-${i}`}
                          className={`text-xs cursor-pointer ${
                            checkedItems.has(i)
                              ? "text-gray-500 line-through"
                              : "text-gray-200"
                          }`}
                        >
                          {item}
                        </label>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {/* Right: Files to check */}
            <div>
              {filesToCheck.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">
                    Files to look at
                  </div>
                  <ul className="space-y-0.5">
                    {filesToCheck.map((path, i) => (
                      <li
                        key={i}
                        className="text-xs font-mono text-gray-300 break-all"
                      >
                        {path}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {task.acceptance_criteria.length > 0 && (
                <div className={filesToCheck.length > 0 ? "mt-3" : ""}>
                  <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">
                    Acceptance criteria
                  </div>
                  <ul className="space-y-0.5">
                    {task.acceptance_criteria.map((c, i) => (
                      <li key={i} className="text-xs text-gray-400">
                        • {c}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>

          {/* Reject form when active */}
          {showRejectForm && (
            <div className="mt-3 p-2 border border-line rounded bg-black/30">
              <label
                htmlFor="reject-feedback"
                className="block text-[10px] uppercase tracking-wider text-gray-500 mb-1"
              >
                What needs to change?
              </label>
              <textarea
                id="reject-feedback"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder="e.g., The canvas is rendering but it's 480x270 instead of 960x540. Check the width/height attributes."
                rows={3}
                className="w-full text-xs bg-black/50 border border-line rounded px-2 py-1.5 text-gray-200 font-mono"
                disabled={submitting}
              />
            </div>
          )}

          {error && (
            <div className="mt-2 text-xs text-red-400">{error}</div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex flex-col gap-1.5 shrink-0 w-36">
          {!showRejectForm ? (
            <>
              <button
                type="button"
                onClick={handleApprove}
                disabled={submitting}
                className={`px-3 py-2 text-xs font-semibold rounded transition-colors ${
                  allChecked
                    ? "bg-emerald-600 hover:bg-emerald-500 text-white"
                    : "bg-emerald-700/70 hover:bg-emerald-600/80 text-white"
                } disabled:opacity-50`}
                title={
                  allChecked
                    ? "All items checked — approve"
                    : "You can approve without checking everything, but go ahead and verify each item first."
                }
              >
                {submitting ? "..." : "Approve"}
              </button>
              <button
                type="button"
                onClick={() => setShowRejectForm(true)}
                disabled={submitting}
                className="px-3 py-2 text-xs bg-red-900/60 hover:bg-red-800/80 text-red-100 rounded disabled:opacity-50"
              >
                Needs changes
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={handleReject}
                disabled={submitting || !feedback.trim()}
                className="px-3 py-2 text-xs font-semibold bg-red-600 hover:bg-red-500 text-white rounded disabled:opacity-50"
              >
                {submitting ? "..." : "Send back"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowRejectForm(false);
                  setFeedback("");
                  setError(null);
                }}
                disabled={submitting}
                className="px-3 py-2 text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 rounded"
              >
                Cancel
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
