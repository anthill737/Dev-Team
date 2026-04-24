import { useEffect, useState } from "react";
import { createProject, listProjects } from "../lib/api";
import type { ProjectSummary } from "../lib/types";

interface Props {
  onSelect: (projectId: string) => void;
}

export function ProjectList({ onSelect }: Props) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

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
  }, []);

  return (
    <div className="h-full max-w-3xl mx-auto p-8">
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-2xl font-semibold">Projects</h1>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="text-sm bg-accent text-black font-medium px-3 py-1.5 rounded hover:bg-amber-400"
        >
          New project
        </button>
      </div>

      {loading ? (
        <div className="text-sm text-gray-500">Loading...</div>
      ) : projects.length === 0 ? (
        <div className="bg-panel border border-line rounded-lg p-10 text-center">
          <p className="text-gray-400 mb-4">No projects yet.</p>
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="text-sm bg-accent text-black font-medium px-3 py-1.5 rounded hover:bg-amber-400"
          >
            Start your first project
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          {projects.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => onSelect(p.id)}
              className="w-full text-left bg-panel border border-line rounded-lg p-4 hover:border-gray-600 transition-colors"
            >
              <div className="flex items-baseline justify-between">
                <div className="font-medium">{p.name}</div>
                <StatusBadge status={p.status} />
              </div>
              <div className="text-xs text-gray-500 mt-1 font-mono">{p.root_path}</div>
              <div className="text-xs text-gray-600 mt-2 flex gap-4">
                <span>{p.tokens_used.toLocaleString()} tokens used</span>
                <span>{p.tasks_completed} tasks done</span>
              </div>
            </button>
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
  return <span className={`text-xs px-2 py-0.5 rounded ${color}`}>{status}</span>;
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
  const [wallClockMode, setWallClockMode] = useState<"unlimited" | "hours">("unlimited");
  const [hours, setHours] = useState(1);
  const [tokenBudget, setTokenBudget] = useState(2_000_000);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = name.trim() && path.trim() && !submitting;

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const project = await createProject({
        name: name.trim(),
        root_path: path.trim(),
        project_token_budget: tokenBudget,
        max_wall_clock_seconds: wallClockMode === "hours" ? hours * 3600 : null,
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
      <div className="bg-panel border border-line rounded-lg p-6 w-full max-w-md">
        <h2 className="text-lg font-semibold mb-4">New project</h2>

        <label className="block text-sm font-medium mb-1">Project name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="My SaaS MVP"
          className="w-full bg-ink border border-line rounded px-3 py-2 text-sm mb-4 focus:outline-none focus:border-accent"
        />

        <label className="block text-sm font-medium mb-1">Project directory (absolute path)</label>
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="/host-home/code/my-saas"
          className="w-full bg-ink border border-line rounded px-3 py-2 text-sm font-mono mb-1 focus:outline-none focus:border-accent"
        />
        <p className="text-xs text-gray-500 mb-4">
          Your home directory is mounted at <code className="bg-ink px-1">/host-home</code> inside
          the container.
        </p>

        <label className="block text-sm font-medium mb-1">How long should the team work?</label>
        <div className="flex gap-2 mb-4">
          <button
            type="button"
            onClick={() => setWallClockMode("unlimited")}
            className={`flex-1 text-sm py-1.5 rounded border ${
              wallClockMode === "unlimited"
                ? "bg-accent text-black border-accent"
                : "border-line text-gray-300 hover:border-gray-600"
            }`}
          >
            Until complete
          </button>
          <button
            type="button"
            onClick={() => setWallClockMode("hours")}
            className={`flex-1 text-sm py-1.5 rounded border ${
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
            onChange={(e) => setHours(Number(e.target.value))}
            className="w-full bg-ink border border-line rounded px-3 py-2 text-sm mb-4"
          />
        )}

        <label className="block text-sm font-medium mb-1">
          Project token budget: {tokenBudget.toLocaleString()}
        </label>
        <input
          type="range"
          min={200_000}
          max={10_000_000}
          step={100_000}
          value={tokenBudget}
          onChange={(e) => setTokenBudget(Number(e.target.value))}
          className="w-full mb-4"
        />

        {error && (
          <div className="mb-4 text-sm text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded hover:bg-ink"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={submit}
            className="text-sm bg-accent text-black font-medium px-3 py-1.5 rounded disabled:opacity-40 hover:bg-amber-400"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
