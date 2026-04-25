import { useEffect, useState } from "react";
import { KeySetup } from "./components/KeySetup";
import { ProjectList } from "./components/ProjectList";
import { ProjectWorkspace } from "./components/ProjectWorkspace";
import { sessionStatus } from "./lib/api";

type Screen =
  | { kind: "loading" }
  | { kind: "key" }
  | { kind: "projects" }
  | { kind: "workspace"; projectId: string };

export default function App() {
  const [screen, setScreen] = useState<Screen>({ kind: "loading" });

  useEffect(() => {
    sessionStatus()
      .then((s) => {
        // In claude_code mode, the user's auth comes from their `claude` CLI
        // subscription config — there's no API key to set, and the backend
        // doesn't read one. Skip KeySetup entirely. If the user's `claude`
        // CLI isn't authenticated, they'll see a clear error from the agent
        // runner on first action; we don't preflight that here because it
        // would slow startup and create a dependency on subprocess spawn.
        if (s.runner === "claude_code") {
          setScreen({ kind: "projects" });
        } else {
          setScreen(s.has_key ? { kind: "projects" } : { kind: "key" });
        }
      })
      .catch(() => setScreen({ kind: "key" }));
  }, []);

  if (screen.kind === "loading") {
    return (
      <div className="h-full flex items-center justify-center text-sm text-gray-500">
        Loading...
      </div>
    );
  }

  if (screen.kind === "key") {
    return <KeySetup onSuccess={() => setScreen({ kind: "projects" })} />;
  }

  if (screen.kind === "projects") {
    return <ProjectList onSelect={(projectId) => setScreen({ kind: "workspace", projectId })} />;
  }

  return (
    <ProjectWorkspace
      projectId={screen.projectId}
      onBack={() => setScreen({ kind: "projects" })}
    />
  );
}
