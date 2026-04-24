// Tests for TaskReviewPanel.
//
// Focused on real behaviors, not implementation mirrors:
//   - Approve button calls reviewTask(projectId, taskId, true)
//   - Reject button calls reviewTask(projectId, taskId, false, feedbackText)
//   - Reject is blocked when feedback is empty or whitespace-only
//   - Checklist checkboxes toggle independently and reflect state
//   - Approve is always clickable (checklist is guidance, not a gate)
//   - Cancel on reject form resets the feedback text
//   - During API call, buttons are disabled to prevent double-submit
//   - Run command text renders verbatim so copy-paste works

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TaskReviewPanel } from "./TaskReviewPanel";
import type { ProjectDetail, Task } from "../lib/types";
import * as api from "../lib/api";

// ---- Test fixtures ---------------------------------------------------------

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "P1-T1",
    phase: "P1",
    title: "Build the canvas",
    description: "Create a 960x540 canvas",
    acceptance_criteria: ["canvas renders at 960x540", "no console errors"],
    dependencies: [],
    status: "review",
    assigned_to: "coder",
    iterations: 1,
    budget_tokens: 50_000,
    notes: [],
    review_summary: "Built the canvas and game loop",
    review_checklist: [
      "Open index.html in Chrome",
      "Confirm canvas is 960x540",
      "Check console has no errors",
    ],
    review_run_command: "python -m http.server 8000",
    review_files_to_check: ["index.html", "main.js"],
    review_requested_at: 1234567890,
    ...overrides,
  };
}

// ---- Setup -----------------------------------------------------------------

beforeEach(() => {
  vi.restoreAllMocks();
});

// ---- Tests -----------------------------------------------------------------

describe("TaskReviewPanel — approve path", () => {
  it("calls reviewTask(projectId, taskId, true) when Approve is clicked", async () => {
    const onReviewed = vi.fn();
    const spy = vi
      .spyOn(api, "reviewTask")
      .mockResolvedValue({} as never);

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={onReviewed}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^approve$/i }));

    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("proj_test", "P1-T1", true);
    await waitFor(() => expect(onReviewed).toHaveBeenCalled());
  });

  it("surfaces backend errors to the UI without calling onReviewed", async () => {
    const onReviewed = vi.fn();
    vi.spyOn(api, "reviewTask").mockRejectedValue(
      new Error("409: wrong state"),
    );

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={onReviewed}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() => {
      expect(screen.getByText(/wrong state/i)).toBeInTheDocument();
    });
    expect(onReviewed).not.toHaveBeenCalled();
  });
});

describe("TaskReviewPanel — reject path validation", () => {
  it("reveals a feedback form when 'Needs changes' is clicked", async () => {
    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    // Feedback form not visible initially
    expect(
      screen.queryByPlaceholderText(/canvas is rendering/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /needs changes/i }));

    // Now it is
    expect(
      screen.getByPlaceholderText(/canvas is rendering/i),
    ).toBeInTheDocument();
  });

  it("does NOT call reviewTask when reject is attempted with empty feedback", async () => {
    const onReviewed = vi.fn();
    const spy = vi.spyOn(api, "reviewTask");

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={onReviewed}
      />,
    );

    await user.click(screen.getByRole("button", { name: /needs changes/i }));

    // Send back button is disabled while textarea is empty; try clicking anyway
    const sendBack = screen.getByRole("button", { name: /send back/i });
    expect(sendBack).toBeDisabled();

    // Confirm no API call happened
    expect(spy).not.toHaveBeenCalled();
    expect(onReviewed).not.toHaveBeenCalled();
  });

  it("does NOT call reviewTask when feedback is whitespace-only", async () => {
    const spy = vi.spyOn(api, "reviewTask");

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: /needs changes/i }));
    const textarea = screen.getByPlaceholderText(/canvas is rendering/i);
    await user.type(textarea, "   ");

    const sendBack = screen.getByRole("button", { name: /send back/i });
    expect(sendBack).toBeDisabled();
    expect(spy).not.toHaveBeenCalled();
  });

  it("calls reviewTask with trimmed feedback when Send back is clicked", async () => {
    const onReviewed = vi.fn();
    const spy = vi
      .spyOn(api, "reviewTask")
      .mockResolvedValue({} as never);

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={onReviewed}
      />,
    );

    await user.click(screen.getByRole("button", { name: /needs changes/i }));
    const textarea = screen.getByPlaceholderText(/canvas is rendering/i);
    // Include leading/trailing whitespace to verify trimming
    await user.type(textarea, "   Canvas is squashed, width wrong   ");
    await user.click(screen.getByRole("button", { name: /send back/i }));

    expect(spy).toHaveBeenCalledWith(
      "proj_test",
      "P1-T1",
      false,
      "Canvas is squashed, width wrong",
    );
    await waitFor(() => expect(onReviewed).toHaveBeenCalled());
  });
});

describe("TaskReviewPanel — reject form lifecycle", () => {
  it("clears the feedback text when Cancel is clicked and reopened", async () => {
    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    // Open, type, cancel
    await user.click(screen.getByRole("button", { name: /needs changes/i }));
    const textarea = screen.getByPlaceholderText(/canvas is rendering/i);
    await user.type(textarea, "stale feedback from last attempt");
    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Reopen — textarea should be empty, not show the old text
    await user.click(screen.getByRole("button", { name: /needs changes/i }));
    const freshTextarea = screen.getByPlaceholderText(/canvas is rendering/i);
    expect(freshTextarea).toHaveValue("");
  });
});

describe("TaskReviewPanel — checklist interaction", () => {
  it("lets user toggle checklist items independently", async () => {
    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(3);

    // All unchecked initially
    checkboxes.forEach((cb) => expect(cb).not.toBeChecked());

    // Tick the first and third
    await user.click(checkboxes[0]);
    await user.click(checkboxes[2]);

    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[1]).not.toBeChecked();
    expect(checkboxes[2]).toBeChecked();

    // The count indicator should update too
    expect(screen.getByText(/check this \(2\/3\)/i)).toBeInTheDocument();

    // Untick the first
    await user.click(checkboxes[0]);
    expect(checkboxes[0]).not.toBeChecked();
    expect(screen.getByText(/check this \(1\/3\)/i)).toBeInTheDocument();
  });

  it("keeps Approve button enabled even when no items are checked", async () => {
    // Design decision: checklist is guidance, not a gate. User can approve early
    // if they trust their own inspection. This is the rule we committed to.
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    const approve = screen.getByRole("button", { name: /^approve$/i });
    expect(approve).toBeEnabled();
    // Checkboxes all unchecked; button must still be clickable
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.every((cb) => !("checked" in cb) || !(cb as HTMLInputElement).checked)).toBe(
      true,
    );
    expect(approve).toBeEnabled();
  });
});

describe("TaskReviewPanel — submission state", () => {
  it("disables Approve while the API call is in flight", async () => {
    const onReviewed = vi.fn();
    // Return a promise we control so we can observe the in-flight state
    let resolveApi: (value: ProjectDetail | PromiseLike<ProjectDetail>) => void = () => {};
    vi.spyOn(api, "reviewTask").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApi = resolve;
        }),
    );

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={onReviewed}
      />,
    );

    const approve = screen.getByRole("button", { name: /^approve$/i });
    await user.click(approve);

    // During the in-flight call, approve shows "..." and is disabled
    await waitFor(() => expect(approve).toBeDisabled());

    // Resolve the API call — the component should then invoke onReviewed
    resolveApi({} as ProjectDetail);
    await waitFor(() => expect(onReviewed).toHaveBeenCalled());
  });

  it("disables Send back while the reject API call is in flight", async () => {
    let resolveApi: (value: ProjectDetail | PromiseLike<ProjectDetail>) => void = () => {};
    vi.spyOn(api, "reviewTask").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApi = resolve;
        }),
    );

    const user = userEvent.setup();
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: /needs changes/i }));
    await user.type(
      screen.getByPlaceholderText(/canvas is rendering/i),
      "feedback",
    );
    const sendBack = screen.getByRole("button", { name: /send back/i });
    await user.click(sendBack);

    await waitFor(() => expect(sendBack).toBeDisabled());
    resolveApi({} as ProjectDetail);
  });
});

describe("TaskReviewPanel — run command display", () => {
  it("renders the run command verbatim so copy-paste is safe", () => {
    // Important: no text transformations, no smart quotes, no trimming of
    // significant whitespace. What the Coder wrote is what the user copies.
    const command =
      "python -m http.server 8000 && open http://localhost:8000/game/index.html";
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask({ review_run_command: command })}
        onReviewed={() => {}}
      />,
    );

    // Find the <code> element containing the run command
    const el = screen.getByText(command);
    expect(el.tagName).toBe("CODE");
  });

  it("shows a Copy button next to the run command", () => {
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: /^copy$/i })).toBeInTheDocument();
  });
});

describe("TaskReviewPanel — rendering content", () => {
  it("displays the task id, title, and summary", () => {
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    expect(screen.getByText("P1-T1")).toBeInTheDocument();
    expect(screen.getByText("Build the canvas")).toBeInTheDocument();
    expect(
      screen.getByText("Built the canvas and game loop"),
    ).toBeInTheDocument();
  });

  it("renders acceptance criteria and files to check", () => {
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={makeTask()}
        onReviewed={() => {}}
      />,
    );

    // Acceptance criteria are rendered as list items
    expect(screen.getByText(/canvas renders at 960x540/i)).toBeInTheDocument();
    // Files to check
    expect(screen.getByText("index.html")).toBeInTheDocument();
    expect(screen.getByText("main.js")).toBeInTheDocument();
  });

  it("renders gracefully when optional review fields are missing", () => {
    // Edge case: an older task or a review state without all metadata. The panel
    // should still render the approve/reject controls rather than crash.
    const sparseTask = makeTask({
      review_summary: undefined,
      review_checklist: undefined,
      review_run_command: undefined,
      review_files_to_check: undefined,
    });
    render(
      <TaskReviewPanel
        projectId="proj_test"
        task={sparseTask}
        onReviewed={() => {}}
      />,
    );

    // Core controls must still be present
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /needs changes/i }),
    ).toBeInTheDocument();
    // No Copy button since there's no command to copy
    expect(
      screen.queryByRole("button", { name: /^copy$/i }),
    ).not.toBeInTheDocument();
    // No checkboxes since there's no checklist
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});
