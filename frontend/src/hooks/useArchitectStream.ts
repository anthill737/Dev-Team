// useArchitectStream — React hook managing the WebSocket to the backend.
//
// Exposes:
//   • status: connection status
//   • streaming: true while an architect turn is in progress
//   • partialText: architect's in-flight streaming text (accumulates deltas)
//   • toolActivity: log of tool_use / tool_result events for the current turn
//   • tokensThisTurn: tokens used in the current turn
//   • error: last error, if any
//   • send(text): send a user message
//
// On turn_complete, partialText is cleared and toolActivity is reset. The completed turn
// is persisted on the backend and the UI can re-fetch interview log + status.

import { useCallback, useEffect, useRef, useState } from "react";
import type { WsEvent, ProjectStatus } from "../lib/types";

export interface ToolEvent {
  name: string;
  kind: "use" | "result";
  isError?: boolean;
  preview?: string;
  input?: Record<string, unknown>;
  at: number;
}

export interface StreamState {
  status: "connecting" | "open" | "closed" | "error";
  streaming: boolean;
  partialText: string;
  toolActivity: ToolEvent[];
  tokensThisTurn: { input: number; output: number };
  lastStatus: ProjectStatus | null;
  error: string | null;
}

const INITIAL_STATE: StreamState = {
  status: "connecting",
  streaming: false,
  partialText: "",
  toolActivity: [],
  tokensThisTurn: { input: 0, output: 0 },
  lastStatus: null,
  error: null,
};

export function useArchitectStream(projectId: string | null, onTurnComplete?: () => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<StreamState>(INITIAL_STATE);

  // Batch text_delta events to at most one state update per animation frame.
  // Without this, a streaming assistant turn fires 60-100 setState calls per
  // second, each causing a full re-render. Accumulating in a ref and flushing
  // on rAF reduces that to ~60/sec max and typically much less, making streaming
  // visibly smooth instead of jittery.
  const pendingTextRef = useRef<string>("");
  const rafIdRef = useRef<number | null>(null);

  const flushPendingText = () => {
    rafIdRef.current = null;
    const pending = pendingTextRef.current;
    if (!pending) return;
    pendingTextRef.current = "";
    setState((s) => ({ ...s, partialText: s.partialText + pending }));
  };

  useEffect(() => {
    if (!projectId) return;

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws/architect/${projectId}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setState((s) => ({ ...s, status: "open", error: null }));
    };

    ws.onclose = () => {
      setState((s) => ({ ...s, status: "closed", streaming: false }));
    };

    ws.onerror = () => {
      setState((s) => ({
        ...s,
        status: "error",
        error: "WebSocket connection error",
      }));
    };

    ws.onmessage = (ev) => {
      let msg: WsEvent;
      try {
        msg = JSON.parse(ev.data) as WsEvent;
      } catch {
        return;
      }
      // Fast path: text_delta accumulates without setState. We schedule one
      // flush per frame. All other events go through setState immediately so
      // tool calls / turn_complete aren't delayed.
      if (msg.type === "text_delta") {
        pendingTextRef.current += msg.text;
        if (rafIdRef.current === null) {
          rafIdRef.current = requestAnimationFrame(flushPendingText);
        }
        return;
      }
      // For non-text events, flush any pending text first so ordering is preserved
      // (e.g., a tool_use after some text should appear AFTER that text, not before).
      if (pendingTextRef.current) {
        if (rafIdRef.current !== null) {
          cancelAnimationFrame(rafIdRef.current);
          rafIdRef.current = null;
        }
        const pending = pendingTextRef.current;
        pendingTextRef.current = "";
        setState((s) => handleEvent({ ...s, partialText: s.partialText + pending }, msg));
      } else {
        setState((s) => handleEvent(s, msg));
      }
      if (msg.type === "turn_complete" && onTurnComplete) {
        onTurnComplete();
      }
    };

    return () => {
      ws.close();
      wsRef.current = null;
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      pendingTextRef.current = "";
    };
    // onTurnComplete is intentionally omitted — including it would cause WS reconnects
    // whenever the parent re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const send = useCallback((text: string) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setState((s) => ({
      ...s,
      streaming: true,
      partialText: "",
      toolActivity: [],
      tokensThisTurn: { input: 0, output: 0 },
      error: null,
    }));
    ws.send(JSON.stringify({ type: "user_message", text }));
  }, []);

  return { ...state, send };
}

function handleEvent(state: StreamState, msg: WsEvent): StreamState {
  switch (msg.type) {
    case "text_delta":
      return { ...state, partialText: state.partialText + msg.text };
    case "tool_use":
      return {
        ...state,
        toolActivity: [
          ...state.toolActivity,
          { name: msg.name, kind: "use", input: msg.input, at: Date.now() },
        ],
      };
    case "tool_result":
      return {
        ...state,
        toolActivity: [
          ...state.toolActivity,
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
        ...state,
        tokensThisTurn: {
          input: state.tokensThisTurn.input + msg.input_tokens,
          output: state.tokensThisTurn.output + msg.output_tokens,
        },
      };
    case "turn_complete":
      return {
        ...state,
        streaming: false,
        partialText: "",
        lastStatus: msg.status,
      };
    case "error":
      return { ...state, streaming: false, error: msg.message };
    default:
      return state;
  }
}
