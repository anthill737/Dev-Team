// Tests for AgentInspector — specifically the routing/distribution logic.
//
// The user reported "all agent dialogue is showing up under Coder." Backend
// integration tests proved the routing is correct server-side (events are
// stored under the right agent bucket). This test verifies the FRONTEND
// distributes events to the correct tab.
//
// If this test passes and the user still sees the bug, the issue is either
// (a) in the deployed build vs. our source, or (b) something specific to
// their data that our test fixture doesn't capture.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentInspector } from "./AgentInspector";
import type { AgentEvent, AgentRole, AgentSummaryEntry } from "../lib/api";
import * as api from "../lib/api";

// ---- Test fixtures ---------------------------------------------------------

function makeEvent(
  agent: AgentRole,
  seq: number,
  text: string,
): AgentEvent {
  return {
    agent,
    kind: "text_delta",
    payload: { text },
    timestamp: 1000000 + seq,
    seq,
    task_id: null,
  };
}

function makeSummary(perAgent: Partial<Record<AgentRole, number>>): Record<
  AgentRole,
  AgentSummaryEntry
> {
  const empty: AgentSummaryEntry = {
    event_count: 0,
    last_seq: 0,
    last_ts: 0,
    last_kind: null,
  };
  return {
    architect: { ...empty, event_count: perAgent.architect ?? 0 },
    dispatcher: { ...empty, event_count: perAgent.dispatcher ?? 0 },
    coder: { ...empty, event_count: perAgent.coder ?? 0 },
    reviewer: { ...empty, event_count: perAgent.reviewer ?? 0 },
    orchestrator: { ...empty, event_count: perAgent.orchestrator ?? 0 },
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("AgentInspector event distribution", () => {
  it("routes architect events to the architect tab, not the coder tab", async () => {
    // Backend returns events from all four agents. Frontend should bucket
    // them by `event.agent` field, NOT lump them all into Coder.
    const events: AgentEvent[] = [
      makeEvent("architect", 1, "ARCHITECT_MESSAGE"),
      makeEvent("dispatcher", 2, "DISPATCHER_MESSAGE"),
      makeEvent("coder", 3, "CODER_MESSAGE"),
      makeEvent("reviewer", 4, "REVIEWER_MESSAGE"),
    ];

    vi.spyOn(api, "getAgentEvents").mockResolvedValue({
      events,
      latest_seq: 4,
    });
    vi.spyOn(api, "getAgentSummary").mockResolvedValue({
      agents: makeSummary({
        architect: 1,
        dispatcher: 1,
        coder: 1,
        reviewer: 1,
      }),
      latest_seq: 4,
    });

    render(<AgentInspector projectId="proj_test" pollIntervalMs={50} />);

    // Wait for the first poll cycle
    await waitFor(() => {
      expect(screen.getByText(/ARCHITECT_MESSAGE/)).toBeDefined();
    });

    // On the All tab (default), all four messages should be visible
    expect(screen.queryByText(/ARCHITECT_MESSAGE/)).toBeDefined();
    expect(screen.queryByText(/DISPATCHER_MESSAGE/)).toBeDefined();
    expect(screen.queryByText(/CODER_MESSAGE/)).toBeDefined();
    expect(screen.queryByText(/REVIEWER_MESSAGE/)).toBeDefined();

    // Switch to the Architect tab. Only architect's message should remain.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Architect/i }));

    await waitFor(() => {
      expect(screen.queryByText(/ARCHITECT_MESSAGE/)).toBeDefined();
    });
    // Critical: architect tab should NOT show coder/dispatcher/reviewer text
    expect(screen.queryByText(/DISPATCHER_MESSAGE/)).toBeNull();
    expect(screen.queryByText(/CODER_MESSAGE/)).toBeNull();
    expect(screen.queryByText(/REVIEWER_MESSAGE/)).toBeNull();
  });

  it("shows reviewer events on the reviewer tab, not coder", async () => {
    // The most likely manifestation of the user's bug: switch to Reviewer tab
    // and find Coder events bleeding through.
    const events: AgentEvent[] = [
      makeEvent("coder", 1, "CODER_BUILT_THE_THING"),
      makeEvent("reviewer", 2, "REVIEWER_FOUND_BUG"),
    ];

    vi.spyOn(api, "getAgentEvents").mockResolvedValue({
      events,
      latest_seq: 2,
    });
    vi.spyOn(api, "getAgentSummary").mockResolvedValue({
      agents: makeSummary({ coder: 1, reviewer: 1 }),
      latest_seq: 2,
    });

    render(<AgentInspector projectId="proj_test" pollIntervalMs={50} />);

    await waitFor(() => {
      expect(screen.queryByText(/REVIEWER_FOUND_BUG/)).toBeDefined();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Reviewer/i }));

    await waitFor(() => {
      expect(screen.queryByText(/REVIEWER_FOUND_BUG/)).toBeDefined();
    });
    expect(screen.queryByText(/CODER_BUILT_THE_THING/)).toBeNull();
  });

  it("counts each agent's events independently in tab labels", async () => {
    // Tab counts should reflect per-agent buckets, not a shared total.
    vi.spyOn(api, "getAgentEvents").mockResolvedValue({
      events: [],
      latest_seq: 0,
    });
    vi.spyOn(api, "getAgentSummary").mockResolvedValue({
      agents: makeSummary({
        architect: 28,
        dispatcher: 25,
        coder: 700,
        reviewer: 84,
      }),
      latest_seq: 837,
    });

    render(<AgentInspector projectId="proj_test" pollIntervalMs={50} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Architect.*28/ })).toBeDefined();
    });
    expect(screen.getByRole("button", { name: /Dispatcher.*25/ })).toBeDefined();
    expect(screen.getByRole("button", { name: /Coder.*700/ })).toBeDefined();
    expect(screen.getByRole("button", { name: /Reviewer.*84/ })).toBeDefined();
  });
});
