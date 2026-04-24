// useDispatcherStream — one-shot WebSocket hook for a Dispatcher run.
//
// Differs from useArchitectStream: the dispatcher is not a conversation, it's a single
// run that the backend kicks off when the WS opens and closes when done. This hook
// only opens the WS when `shouldRun` is true (i.e., when project status is DISPATCHING
// and we haven't already dispatched).

import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "../lib/types";
import type { ToolEvent } from "./useArchitectStream";

export interface DispatcherStreamState {
  status: "idle" | "connecting" | "running" | "done" | "error";
  partialText: string;
  toolActivity: ToolEvent[];
  tokensUsed: { input: number; output: number };
  error: string | null;
}

const INITIAL: DispatcherStreamState = {
  status: "idle",
  partialText: "",
  toolActivity: [],
  tokensUsed: { input: 0, output: 0 },
  error: null,
};

export function useDispatcherStream(
  projectId: string | null,
  shouldRun: boolean,
  onComplete?: () => void,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<DispatcherStreamState>(INITIAL);

  useEffect(() => {
    if (!projectId || !shouldRun) {
      // If we had a prior run, reset state when shouldRun drops
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }

    // Avoid re-opening if already connected
    if (wsRef.current) return;

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws/dispatcher/${projectId}`;
    setState({ ...INITIAL, status: "connecting" });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setState((s) => ({ ...s, status: "running" }));
    };

    ws.onmessage = (ev) => {
      let msg: WsEvent;
      try {
        msg = JSON.parse(ev.data) as WsEvent;
      } catch {
        return;
      }
      setState((s) => reduce(s, msg));
    };

    ws.onclose = () => {
      setState((s) => (s.status === "running" ? { ...s, status: "done" } : s));
      wsRef.current = null;
      if (onComplete) onComplete();
    };

    ws.onerror = () => {
      setState((s) => ({
        ...s,
        status: "error",
        error: "WebSocket error during dispatcher run",
      }));
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, shouldRun]);

  return state;
}

function reduce(s: DispatcherStreamState, msg: WsEvent): DispatcherStreamState {
  switch (msg.type) {
    case "text_delta":
      return { ...s, partialText: s.partialText + msg.text };
    case "tool_use":
      return {
        ...s,
        toolActivity: [
          ...s.toolActivity,
          { name: msg.name, kind: "use", input: msg.input, at: Date.now() },
        ],
      };
    case "tool_result":
      return {
        ...s,
        toolActivity: [
          ...s.toolActivity,
          {
            name: msg.name,
            kind: "result",
            isError: msg.is_error,
            preview: msg.preview,
            at: Date.now(),
          },
        ],
      };
    case "usage":
      return {
        ...s,
        tokensUsed: {
          input: s.tokensUsed.input + msg.input_tokens,
          output: s.tokensUsed.output + msg.output_tokens,
        },
      };
    case "turn_complete":
      return { ...s, status: "done" };
    case "error":
      return { ...s, status: "error", error: msg.message };
    default:
      return s;
  }
}
