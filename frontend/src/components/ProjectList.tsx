import { useEffect, useState } from "react";
import {
  bulkUpdateTaskBudget,
  createProject,
  deleteProject,
  listProjects,
  openProjectIn,
  updateProject,
} from "../lib/api";
import type { ProjectSummary } from "../lib/types";

interface Props {
  onSelect: (projectId: string) => void;
}

export function ProjectList({ onSelect }: Props) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  // Which project is being confirmed for delete (null = no modal open).
  // Stored as the full ProjectSummary so the modal can show name + path
  // without a second fetch.
  const [deleting, setDeleting] = useState<ProjectSummary | null>(null);
  // Which project is being edited (null = no modal open). Uses summary
  // fields; the modal fetches full detail lazily to get budgets.
  const [editing, setEditing] = useState<ProjectSummary | null>(null);

  const refresh = async () => {
    try {
      setProjects(await listProjects());
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // Poll so the running badge reflects live state without manual refresh.
    // Cheap call — in-memory registry lookup + file reads. 3s feels responsive
    // without thrashing; long-running projects don't emit UI-level changes
    // frequently enough to need faster.
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="h-full max-w-3xl mx-auto p-8">
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-2xl font-semibold">Projects</h1>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="text-[17px] bg-accent text-black font-medium px-3 py-1.5 rounded hover:bg-amber-400"
        >
          New project
        </button>
      </div>

      {loading ? (
        <div className="text-[17px] text-gray-500">Loading...</div>
      ) : projects.length === 0 ? (
        <div className="bg-panel border border-line rounded-lg p-10 text-center">
          <p className="text-gray-400 mb-4">No projects yet.</p>
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="text-[17px] bg-accent text-black font-medium px-3 py-1.5 rounded hover:bg-amber-400"
          >
            Start your first project
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          {projects.map((p) => (
            // Card is a div with a click handler rather than a <button> so
            // the inner delete <button> is valid HTML (buttons can't nest).
            <div
              key={p.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(p.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onSelect(p.id);
              }}
              className="group relative w-full text-left bg-panel border border-line rounded-lg p-4 hover:border-gray-600 transition-colors cursor-pointer"
            >
              <div className="flex items-baseline justify-between">
                <div className="font-medium flex items-center gap-2">
                  {p.name}
                  {p.is_running && (
                    <span
                      className="inline-flex items-center gap-1 text-[13px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300"
                      title="Background execution job running — work continues even when you're not viewing this project."
                    >
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                      running
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={p.status} />
                  {/* Open-in buttons: quick launchers for the project root.
                      Same hover-reveal pattern as gear/trash so the card stays
                      uncluttered until you mouse over it. Each button calls
                      the backend's /open endpoint, which shells out to the OS.
                      stopPropagation prevents the card's onClick (which navs
                      into the project) from firing when you click a button. */}
                  <button
                    type="button"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await openProjectIn(p.id, "explorer");
                      } catch (err) {
                        alert(`Couldn't open in Explorer: ${(err as Error).message}`);
                      }
                    }}
                    title="Open the project folder in Windows Explorer."
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-[15px] text-gray-500 hover:text-gray-200 px-1.5 py-0.5 rounded"
                    aria-label={`Open ${p.name} in Explorer`}
                  >
                    📁
                  </button>
                  <button
                    type="button"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await openProjectIn(p.id, "vscode");
                      } catch (err) {
                        alert(`Couldn't open in VS Code: ${(err as Error).message}`);
                      }
                    }}
                    title="Open the project in VS Code. Requires the `code` command on PATH."
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-[15px] text-gray-500 hover:text-gray-200 px-1.5 py-0.5 rounded font-semibold"
                    aria-label={`Open ${p.name} in VS Code`}
                  >
                    VS
                  </button>
                  <button
                    type="button"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await openProjectIn(p.id, "terminal");
                      } catch (err) {
                        alert(`Couldn't open terminal: ${(err as Error).message}`);
                      }
                    }}
                    title="Open a fresh terminal already CDed into the project folder."
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-[15px] text-gray-500 hover:text-gray-200 px-1.5 py-0.5 rounded font-mono"
                    aria-label={`Open terminal in ${p.name}`}
                  >
                    {">_"}
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setEditing(p);
                    }}
                    title="Project settings — name, path, budgets, time limit, OS."
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-[15px] text-gray-500 hover:text-gray-200 px-1.5 py-0.5 rounded"
                    aria-label={`Settings for ${p.name}`}
                  >
                    ⚙
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      // Don't trigger the card's onClick.
                      e.stopPropagation();
                      setDeleting(p);
                    }}
                    title="Remove this project from Dev Team's list. Your code stays on disk."
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-[15px] text-gray-500 hover:text-red-400 px-1.5 py-0.5 rounded"
                    aria-label={`Delete ${p.name}`}
                  >
                    🗑
                  </button>
                </div>
              </div>
              <div className="text-[15px] text-gray-500 mt-1 font-mono">{p.root_path}</div>
              <div className="text-[15px] text-gray-600 mt-2 flex gap-4">
                <span>{p.tokens_used.toLocaleString()} tokens used</span>
                <span>{p.tasks_completed} tasks done</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <CreateProjectModal
          onClose={() => setShowCreate(false)}
          onCreated={(id) => {
            setShowCreate(false);
            onSelect(id);
          }}
        />
      )}

      {deleting && (
        <DeleteProjectModal
          project={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null);
            refresh();
          }}
        />
      )}

      {editing && (
        <EditProjectModal
          project={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "complete"
      ? "bg-green-900/40 text-green-300"
      : status === "failed" || status === "blocked"
        ? "bg-red-900/40 text-red-300"
        : status === "paused"
          ? "bg-gray-800 text-gray-400"
          : "bg-amber-900/40 text-amber-300";
  return <span className={`text-[15px] px-2 py-0.5 rounded ${color}`}>{status}</span>;
}

function DeleteProjectModal({
  project,
  onClose,
  onDeleted,
}: {
  project: ProjectSummary;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [purge, setPurge] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Running projects can't be deleted — backend refuses and we disable the
  // delete button here so the user sees why upfront.
  const isRunning = project.is_running === true;

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await deleteProject(project.id, purge);
      onDeleted();
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-6 z-50">
      <div className="bg-panel border border-line rounded-lg p-6 w-full max-w-md">
        <h2 className="text-[19px] font-semibold mb-2">Delete project</h2>
        <p className="text-[17px] text-gray-400 mb-1">
          Remove <span className="text-gray-200 font-medium">{project.name}</span> from
          Dev Team?
        </p>
        <p className="text-[15px] text-gray-500 font-mono mb-4 break-all">{project.root_path}</p>

        <div className="bg-ink border border-line rounded px-3 py-2 mb-4 text-[15px] text-gray-400">
          By default, <span className="text-gray-200">only the entry in Dev Team's list
          is removed.</span> Your code on disk is not touched.
        </div>

        <label className="flex items-start gap-2 mb-4 cursor-pointer">
          <input
            type="checkbox"
            checked={purge}
            onChange={(e) => setPurge(e.target.checked)}
            className="mt-1"
          />
          <span className="text-[17px]">
            Also delete Dev Team's state folder (<code className="bg-ink px-1">.devteam/</code>)
            <div className="text-[15px] text-gray-500 mt-0.5">
              Removes the plan, task list, decisions log, and review scratch for this
              project. Your own code files are still untouched.
            </div>
          </span>
        </label>

        {isRunning && (
          <div className="mb-4 text-[15px] text-amber-300 bg-amber-950/40 border border-amber-900/50 rounded px-3 py-2">
            This project has a running execution job. Pause it or wait for it to finish
            before deleting.
          </div>
        )}

        {error && (
          <div className="mb-4 text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="text-[17px] px-3 py-1.5 rounded hover:bg-ink disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={isRunning || submitting}
            onClick={submit}
            className="text-[17px] bg-red-800 hover:bg-red-700 text-white font-medium px-3 py-1.5 rounded disabled:opacity-40"
          >
            {submitting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function EditProjectModal({
  project,
  onClose,
  onSaved,
}: {
  project: ProjectSummary;
  onClose: () => void;
  onSaved: () => void;
}) {
  // Fetch the full detail on mount to get budgets (summary doesn't carry them).
  // Keeping budgets in local state as strings so the user can clear the input
  // mid-edit without the field snapping to 0.
  const [name, setName] = useState(project.name);
  const [rootPath, setRootPath] = useState(project.root_path);
  const [projectBudget, setProjectBudget] = useState<string>("");
  const [taskBudget, setTaskBudget] = useState<string>("");
  // Tracks the task budget AT LOAD TIME so submit can detect a real change
  // and auto-propagate to existing unfinished tasks. Null until the detail
  // fetch completes, so we don't misfire on a user who just opens the modal.
  const [originalTaskBudget, setOriginalTaskBudget] = useState<number | null>(null);
  const [maxIterations, setMaxIterations] = useState<string>("");
  const [wallClockMode, setWallClockMode] = useState<"unlimited" | "hours">("unlimited");
  const [hours, setHours] = useState<string>("1");
  const [userPlatform, setUserPlatform] = useState<"windows" | "macos" | "linux">("linux");
  // Per-agent model choice. The string is either a real model id (override)
  // or the sentinel "default" to clear the override and use global default.
  // Loaded from detail on mount; user changes via the dropdowns.
  const [modelArchitect, setModelArchitect] = useState<string>("default");
  const [modelDispatcher, setModelDispatcher] = useState<string>("default");
  const [modelCoder, setModelCoder] = useState<string>("default");
  const [modelReviewer, setModelReviewer] = useState<string>("default");
  // Catalog loaded from backend so dropdowns show valid options + global
  // default labels. Null until first fetch resolves.
  const [catalog, setCatalog] = useState<{
    choices: { string: string; label: string; cost_hint: string }[];
    defaults: { architect: string; dispatcher: string; coder: string; reviewer: string };
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // root_path editing is dangerous: refuse to offer it on running projects.
  const rootPathEditable = !project.is_running;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Fetch detail + catalog in parallel — both are cheap reads.
        // The api module's getProject returns ProjectDetail which carries the
        // override_model_* fields we use to distinguish "explicit override"
        // from "no override; happens to match default."
        const mod = await import("../lib/api");
        const [detail, cat] = await Promise.all([
          mod.getProject(project.id),
          mod.getModelCatalog(),
        ]);
        if (cancelled) return;
        setProjectBudget(String(detail.project_token_budget));
        setTaskBudget(String(detail.default_task_token_budget));
        setOriginalTaskBudget(detail.default_task_token_budget);
        setMaxIterations(String(detail.max_task_iterations));
        if (detail.max_wall_clock_seconds) {
          setWallClockMode("hours");
          setHours(String(detail.max_wall_clock_seconds / 3600));
        } else {
          setWallClockMode("unlimited");
        }
        if (detail.user_platform) setUserPlatform(detail.user_platform);
        // Seed model dropdowns: null override → "default" sentinel; else
        // the actual model string. ?? handles both null and undefined for
        // older projects that predate these fields.
        setCatalog(cat);
        setModelArchitect(detail.override_model_architect ?? "default");
        setModelDispatcher(detail.override_model_dispatcher ?? "default");
        setModelCoder(detail.override_model_coder ?? "default");
        setModelReviewer(detail.override_model_reviewer ?? "default");
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project.id]);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      // Build the patch body: only include fields the user actually changed or
      // that are safe to resend. Numeric fields get parsed here; NaN means the
      // user cleared the input, which we treat as "don't send" rather than
      // bouncing on the server.
      const args: Parameters<typeof updateProject>[1] = {};
      if (name.trim() !== project.name) args.name = name.trim();
      if (rootPathEditable && rootPath.trim() !== project.root_path) {
        args.root_path = rootPath.trim();
      }
      const pb = Number(projectBudget);
      if (Number.isFinite(pb) && pb > 0) args.project_token_budget = pb;
      const tb = Number(taskBudget);
      if (Number.isFinite(tb) && tb > 0) args.default_task_token_budget = tb;
      const mi = Number(maxIterations);
      if (Number.isFinite(mi) && mi > 0) args.max_task_iterations = mi;
      if (wallClockMode === "unlimited") {
        args.clear_max_wall_clock = true;
      } else {
        const h = Number(hours);
        if (Number.isFinite(h) && h > 0) args.max_wall_clock_seconds = Math.round(h * 3600);
      }
      // Always send the platform selection; cheap to patch and the user
      // probably expects changes to apply even if they didn't "touch" anything.
      args.user_platform = userPlatform;

      // Send model selections. The dropdown value is either "default" (clear
      // override) or a real model id. Backend handles both. We send all four
      // every time — cheap and keeps the on-disk state in sync with what the
      // user sees in the modal.
      args.model_architect = modelArchitect;
      args.model_dispatcher = modelDispatcher;
      args.model_coder = modelCoder;
      args.model_reviewer = modelReviewer;

      await updateProject(project.id, args);

      // If the default task budget changed, propagate to all existing
      // non-done tasks too. This matches user intent: "I set a new per-task
      // budget" should affect the tasks already in flight, not just future
      // ones. The bulk endpoint skips done tasks so completed work isn't
      // retroactively touched. Done AFTER updateProject so failing to
      // propagate doesn't prevent the core settings save from sticking.
      if (
        args.default_task_token_budget !== undefined &&
        originalTaskBudget !== null &&
        args.default_task_token_budget !== originalTaskBudget
      ) {
        try {
          await bulkUpdateTaskBudget(project.id, args.default_task_token_budget);
        } catch (bulkErr) {
          // Surface but don't block — the settings were saved successfully,
          // only the propagation failed. User can try again or edit tasks
          // individually.
          console.error("Bulk task budget update failed:", bulkErr);
        }
      }

      onSaved();
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-6 z-50">
      <div className="bg-panel border border-line rounded-lg p-6 w-full max-w-md max-h-[90vh] overflow-y-auto">
        <h2 className="text-[19px] font-semibold mb-4">Edit project</h2>

        {loading ? (
          <div className="text-[17px] text-gray-500">Loading current settings…</div>
        ) : (
          <>
            <label className="block text-[17px] font-medium mb-1">Project name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4 focus:outline-none focus:border-accent"
            />

            <label className="block text-[17px] font-medium mb-1">Project directory</label>
            <input
              value={rootPath}
              onChange={(e) => setRootPath(e.target.value)}
              disabled={!rootPathEditable}
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] font-mono mb-1 focus:outline-none focus:border-accent disabled:opacity-50"
            />
            <p className="text-[15px] text-gray-500 mb-4">
              {rootPathEditable ? (
                <>
                  If you move the folder to a new location, update this after
                  moving. The <code className="bg-ink px-1">.devteam/</code> subfolder
                  must move with it.
                </>
              ) : (
                <>Cannot change while the project is running. Pause first.</>
              )}
            </p>

            <label className="block text-[17px] font-medium mb-1">
              Project token budget
            </label>
            <input
              type="number"
              min={1}
              value={projectBudget}
              onChange={(e) => setProjectBudget(e.target.value)}
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4"
            />

            <label className="block text-[17px] font-medium mb-1">
              Default per-task token budget
            </label>
            <input
              type="number"
              min={1}
              value={taskBudget}
              onChange={(e) => setTaskBudget(e.target.value)}
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4"
            />

            <label className="block text-[17px] font-medium mb-1">
              Max iterations per task
            </label>
            <input
              type="number"
              min={1}
              value={maxIterations}
              onChange={(e) => setMaxIterations(e.target.value)}
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4"
            />

            <label className="block text-[17px] font-medium mb-1">
              Wall clock limit
            </label>
            <div className="flex gap-2 mb-2">
              <button
                type="button"
                onClick={() => setWallClockMode("unlimited")}
                className={`flex-1 text-[17px] py-1.5 rounded border ${
                  wallClockMode === "unlimited"
                    ? "bg-accent text-black border-accent"
                    : "border-line text-gray-300 hover:border-gray-600"
                }`}
              >
                Unlimited
              </button>
              <button
                type="button"
                onClick={() => setWallClockMode("hours")}
                className={`flex-1 text-[17px] py-1.5 rounded border ${
                  wallClockMode === "hours"
                    ? "bg-accent text-black border-accent"
                    : "border-line text-gray-300 hover:border-gray-600"
                }`}
              >
                For N hours
              </button>
            </div>
            {wallClockMode === "hours" && (
              <input
                type="number"
                min={0.5}
                step={0.5}
                value={hours}
                onChange={(e) => setHours(e.target.value)}
                className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4"
              />
            )}

            <label className="block text-[17px] font-medium mb-1">Your operating system</label>
            <select
              value={userPlatform}
              onChange={(e) =>
                setUserPlatform(e.target.value as "windows" | "macos" | "linux")
              }
              className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-1"
            >
              <option value="windows">Windows (PowerShell)</option>
              <option value="macos">macOS (zsh/bash)</option>
              <option value="linux">Linux (bash)</option>
            </select>
            <p className="text-[15px] text-gray-500 mb-4">
              Controls shell syntax the Coder and Reviewer use for commands you're
              meant to run yourself (verification steps, task notes). Auto-detected
              when you created the project — only change it if you moved the project
              to a different OS.
            </p>

            {catalog && (
              <ModelAssignmentsSection
                catalog={catalog}
                architect={modelArchitect}
                dispatcher={modelDispatcher}
                coder={modelCoder}
                reviewer={modelReviewer}
                onArchitect={setModelArchitect}
                onDispatcher={setModelDispatcher}
                onCoder={setModelCoder}
                onReviewer={setModelReviewer}
              />
            )}
          </>
        )}

        {error && (
          <div className="mb-4 text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="text-[17px] px-3 py-1.5 rounded hover:bg-ink disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={loading || submitting}
            onClick={submit}
            className="text-[17px] bg-accent text-black font-medium px-3 py-1.5 rounded disabled:opacity-40 hover:bg-amber-400"
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function CreateProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (projectId: string) => void;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  // Defaults match backend config (config.py). Duplicated because the create
  // dialog is the first interaction — we can't round-trip the defaults from
  // the server without an extra request. Keep in sync if backend defaults change.
  const [projectBudget, setProjectBudget] = useState<string>("5000000");
  const [taskBudget, setTaskBudget] = useState<string>("150000");
  const [maxIterations, setMaxIterations] = useState<string>("8");
  const [wallClockMode, setWallClockMode] = useState<"unlimited" | "hours">("unlimited");
  const [hours, setHours] = useState<string>("1");
  // Per-agent model picks. New projects start with "default" for everything;
  // user can override via the dropdowns before clicking Create.
  const [modelArchitect, setModelArchitect] = useState<string>("default");
  const [modelDispatcher, setModelDispatcher] = useState<string>("default");
  const [modelCoder, setModelCoder] = useState<string>("default");
  const [modelReviewer, setModelReviewer] = useState<string>("default");
  const [catalog, setCatalog] = useState<{
    choices: { string: string; label: string; cost_hint: string }[];
    defaults: { architect: string; dispatcher: string; coder: string; reviewer: string };
  } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the model catalog once. Cheap; no auth dependency beyond the api key
  // we always have at this point.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mod = await import("../lib/api");
        const cat = await mod.getModelCatalog();
        if (!cancelled) setCatalog(cat);
      } catch {
        // Silent — without catalog, the model section just doesn't render and
        // create still works using defaults. Not blocking.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const canSubmit = name.trim() && path.trim() && !submitting;

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      // Parse each numeric field; fall back to backend defaults if the user
      // cleared it entirely. The backend applies its own defaults for any
      // field omitted from the request.
      const pb = Number(projectBudget);
      const tb = Number(taskBudget);
      const mi = Number(maxIterations);
      const h = Number(hours);

      const project = await createProject({
        name: name.trim(),
        root_path: path.trim(),
        project_token_budget: Number.isFinite(pb) && pb > 0 ? pb : undefined,
        default_task_token_budget: Number.isFinite(tb) && tb > 0 ? tb : undefined,
        max_task_iterations: Number.isFinite(mi) && mi > 0 ? mi : undefined,
        max_wall_clock_seconds:
          wallClockMode === "hours" && Number.isFinite(h) && h > 0
            ? Math.round(h * 3600)
            : null,
        // Per-agent model picks. Skip "default" — it means "no override",
        // and omitting the field has the same effect on the backend (cleaner
        // request body too). Only send actual model strings.
        ...(modelArchitect !== "default" ? { model_architect: modelArchitect } : {}),
        ...(modelDispatcher !== "default" ? { model_dispatcher: modelDispatcher } : {}),
        ...(modelCoder !== "default" ? { model_coder: modelCoder } : {}),
        ...(modelReviewer !== "default" ? { model_reviewer: modelReviewer } : {}),
      });
      onCreated(project.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-6 z-50">
      <div className="bg-panel border border-line rounded-lg p-6 w-full max-w-md max-h-[90vh] overflow-y-auto">
        <h2 className="text-[19px] font-semibold mb-4">New project</h2>

        <label className="block text-[17px] font-medium mb-1">Project name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="My SaaS MVP"
          className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4 focus:outline-none focus:border-accent"
        />

        <label className="block text-[17px] font-medium mb-1">Project directory (absolute path)</label>
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="C:\\Users\\you\\Projects\\my-saas"
          className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] font-mono mb-1 focus:outline-none focus:border-accent"
        />
        <p className="text-[15px] text-gray-500 mb-4">
          Where the Coder will work. Created if it doesn't exist.
        </p>

        <label className="block text-[17px] font-medium mb-1">Project token budget</label>
        <input
          type="number"
          min={1}
          value={projectBudget}
          onChange={(e) => setProjectBudget(e.target.value)}
          className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-1"
        />
        <p className="text-[15px] text-gray-500 mb-4">
          Hard ceiling across the whole project. Execution stops if this is hit.
        </p>

        <label className="block text-[17px] font-medium mb-1">Default per-task token budget</label>
        <input
          type="number"
          min={1}
          value={taskBudget}
          onChange={(e) => setTaskBudget(e.target.value)}
          className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-1"
        />
        <p className="text-[15px] text-gray-500 mb-4">
          Each task gets this budget. The Coder stops trying if it runs out.
        </p>

        <label className="block text-[17px] font-medium mb-1">Max iterations per task</label>
        <input
          type="number"
          min={1}
          value={maxIterations}
          onChange={(e) => setMaxIterations(e.target.value)}
          className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-1"
        />
        <p className="text-[15px] text-gray-500 mb-4">
          How many times the Coder retries a failed task before escalating.
        </p>

        <label className="block text-[17px] font-medium mb-1">Wall clock limit</label>
        <div className="flex gap-2 mb-4">
          <button
            type="button"
            onClick={() => setWallClockMode("unlimited")}
            className={`flex-1 text-[17px] py-1.5 rounded border ${
              wallClockMode === "unlimited"
                ? "bg-accent text-black border-accent"
                : "border-line text-gray-300 hover:border-gray-600"
            }`}
          >
            Unlimited
          </button>
          <button
            type="button"
            onClick={() => setWallClockMode("hours")}
            className={`flex-1 text-[17px] py-1.5 rounded border ${
              wallClockMode === "hours"
                ? "bg-accent text-black border-accent"
                : "border-line text-gray-300 hover:border-gray-600"
            }`}
          >
            For N hours
          </button>
        </div>
        {wallClockMode === "hours" && (
          <input
            type="number"
            min={0.5}
            step={0.5}
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] mb-4"
          />
        )}

        {catalog && (
          <ModelAssignmentsSection
            catalog={catalog}
            architect={modelArchitect}
            dispatcher={modelDispatcher}
            coder={modelCoder}
            reviewer={modelReviewer}
            onArchitect={setModelArchitect}
            onDispatcher={setModelDispatcher}
            onCoder={setModelCoder}
            onReviewer={setModelReviewer}
          />
        )}

        {error && (
          <div className="mb-4 text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={onClose}
            className="text-[17px] px-3 py-1.5 rounded hover:bg-ink"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={submit}
            className="text-[17px] bg-accent text-black font-medium px-3 py-1.5 rounded disabled:opacity-40 hover:bg-amber-400"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Reusable section: four dropdowns for per-agent model assignment. Used by
// both EditProjectModal and CreateProjectModal so the UI stays in sync.
//
// Each dropdown shows:
//   - "(default: <Label>)" as the first option, mapping to the sentinel
//     "default" string. Picking this clears any per-project override.
//   - One <option> per model in the catalog, value=model string, label=
//     friendly name. Tooltip on the option shows the cost hint.
//
// The handlers receive the chosen string. "default" or a model id — caller
// passes that straight through to the API.
function ModelAssignmentsSection({
  catalog,
  architect,
  dispatcher,
  coder,
  reviewer,
  onArchitect,
  onDispatcher,
  onCoder,
  onReviewer,
}: {
  catalog: {
    choices: { string: string; label: string; cost_hint: string }[];
    defaults: { architect: string; dispatcher: string; coder: string; reviewer: string };
  };
  architect: string;
  dispatcher: string;
  coder: string;
  reviewer: string;
  onArchitect: (v: string) => void;
  onDispatcher: (v: string) => void;
  onCoder: (v: string) => void;
  onReviewer: (v: string) => void;
}) {
  // Helper: find the friendly label for a given default model string.
  // Falls back to the raw string if a default in config doesn't appear in
  // the catalog (shouldn't happen, but defensive).
  const labelOf = (str: string) =>
    catalog.choices.find((c) => c.string === str)?.label ?? str;

  return (
    <div className="mb-4">
      <label className="block text-[17px] font-medium mb-1">Model assignments</label>
      <p className="text-[15px] text-gray-500 mb-3">
        Pick a model per agent. (default) uses the global default for that role.
        Architect and Reviewer benefit most from Opus; Coder/Dispatcher are
        usually fine on Sonnet. Haiku is cheapest but risks plan/code quality.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <ModelDropdown
          label="Architect"
          hint="Plans and interviews. Opus recommended."
          value={architect}
          onChange={onArchitect}
          defaultLabel={labelOf(catalog.defaults.architect)}
          choices={catalog.choices}
        />
        <ModelDropdown
          label="Dispatcher"
          hint="Decomposes phases into tasks."
          value={dispatcher}
          onChange={onDispatcher}
          defaultLabel={labelOf(catalog.defaults.dispatcher)}
          choices={catalog.choices}
        />
        <ModelDropdown
          label="Coder"
          hint="Writes the code. Bulk of the workload."
          value={coder}
          onChange={onCoder}
          defaultLabel={labelOf(catalog.defaults.coder)}
          choices={catalog.choices}
        />
        <ModelDropdown
          label="Reviewer"
          hint="Verifies the Coder's work. Opus recommended."
          value={reviewer}
          onChange={onReviewer}
          defaultLabel={labelOf(catalog.defaults.reviewer)}
          choices={catalog.choices}
        />
      </div>
    </div>
  );
}

function ModelDropdown({
  label,
  hint,
  value,
  onChange,
  defaultLabel,
  choices,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  defaultLabel: string;
  choices: { string: string; label: string; cost_hint: string }[];
}) {
  return (
    <div>
      <label className="block text-[15px] font-medium mb-1">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-ink border border-line rounded px-2 py-1.5 text-[15px]"
        title={hint}
      >
        <option value="default">(default: {defaultLabel})</option>
        {choices.map((c) => (
          <option key={c.string} value={c.string} title={c.cost_hint}>
            {c.label}
          </option>
        ))}
      </select>
    </div>
  );
}
