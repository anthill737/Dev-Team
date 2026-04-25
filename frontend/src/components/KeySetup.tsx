import { useState } from "react";
import { setApiKey } from "../lib/api";

interface Props {
  onSuccess: () => void;
}

export function KeySetup({ onSuccess }: Props) {
  const [key, setKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = key.trim().length > 10 && !submitting;

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await setApiKey(key.trim());
      onSuccess();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="h-full flex items-center justify-center p-8">
      <div className="w-full max-w-lg">
        <h1 className="text-2xl font-semibold mb-1">Dev Team</h1>
        <p className="text-[17px] text-gray-400 mb-8">
          Autonomous software development, powered by Claude.
        </p>

        <div className="bg-panel border border-line rounded-lg p-6">
          <label className="block text-[17px] font-medium mb-2">Anthropic API key</label>
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && canSubmit) handleSubmit();
            }}
            placeholder="sk-ant-..."
            autoFocus
            className="w-full bg-ink border border-line rounded px-3 py-2 text-[17px] font-mono focus:outline-none focus:border-accent"
          />
          <p className="text-[15px] text-gray-500 mt-2">
            Get a key at{" "}
            <a
              href="https://console.anthropic.com"
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              console.anthropic.com
            </a>
            . Your key stays in memory on this machine and is never written to disk.
          </p>

          {error && (
            <div className="mt-4 text-[17px] text-red-400 bg-red-950/40 border border-red-900/50 rounded px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="button"
            disabled={!canSubmit}
            onClick={handleSubmit}
            className="mt-6 w-full bg-accent text-black font-medium py-2 rounded disabled:opacity-40 disabled:cursor-not-allowed hover:bg-amber-400 transition-colors"
          >
            {submitting ? "Validating..." : "Continue"}
          </button>
        </div>
      </div>
    </div>
  );
}
