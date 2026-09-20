/**
 * Re-running an execution from the executions list.
 *
 * A re-run replays the inputs the row recorded through the execute route, which
 * starts the workflow's *active* version. This mounts the panel against a list
 * that holds one reproducible row and one row that is missing the version it
 * ran, and asserts the panel offers the re-run only for the former, sends the
 * recorded inputs and names what is missing instead of guessing on the latter.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  const missingRoute = async () => {
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  };
  return {
    ...actual,
    request: vi.fn(missingRoute),
    requestUpload: vi.fn(missingRoute),
    requestBlob: vi.fn(missingRoute),
  };
});

import { request } from "../../../api/request";
import type { Execution } from "../../../api/modules/workbuddyRuntime";
import ExecutionsPanel from "./ExecutionsPanel";

const RERUN = "workbuddy.workflows.executions.rerun";
const MISSING_VERSION = "workbuddy.workflows.executions.rerunMissingVersion";
const STARTED = "workbuddy.workflows.executions.rerunStarted";

const WORKFLOW_ID = "44444444-4444-4444-8444-444444444444";

function row(id: string, workflowVersionId: string): Execution {
  return {
    id,
    workflow_id: WORKFLOW_ID,
    workflow_version_id: workflowVersionId,
    status: "failed",
    trigger_type: "api",
    inputs: { bid: "BID-42" },
    outputs: {},
    error_code: "TOOL_TIMEOUT",
    error_message: "the tool call timed out",
    created_by_user_id: 7,
    created_at: "2026-09-01T10:00:00+00:00",
    started_at: "2026-09-01T10:00:01+00:00",
    finished_at: "2026-09-01T10:00:09+00:00",
  };
}

/** Reproducible: it carries the version it ran, so the re-run can be offered. */
const REPRODUCIBLE = row("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "55555555-5555-4555-8555-555555555555");
/** The server sent no version for this row, so nothing can be reproduced. */
const WITHOUT_VERSION = row("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "");

const onExecutionAccepted = vi.fn();

beforeEach(() => {
  onExecutionAccepted.mockClear();
  // Call history is per file unless cleared, and the first case counts list reads.
  vi.mocked(request).mockClear();
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (String(path).startsWith("/v1/executions?")) {
      return {
        data: { items: [REPRODUCIBLE, WITHOUT_VERSION] },
        request_id: "test",
      };
    }
    if (path === `/v1/workflows/${WORKFLOW_ID}/execute`) {
      return {
        data: { ...REPRODUCIBLE, id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" },
        request_id: "test",
      };
    }
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
});

function renderPanel() {
  render(
    <ExecutionsPanel
      workflowId={WORKFLOW_ID}
      onSelectWorkflow={() => {}}
      options={[{ value: WORKFLOW_ID, label: "Quarterly report" }]}
      optionsLoading={false}
      onExecutionAccepted={onExecutionAccepted}
    />,
  );
}

describe("executions panel re-run", () => {
  it("replays the inputs the row recorded and refreshes the list", async () => {
    const user = userEvent.setup();
    renderPanel();

    const rerunButtons = await screen.findAllByRole("button", { name: RERUN });
    expect(rerunButtons[0]).toBeEnabled();
    await user.click(rerunButtons[0]);
    await user.click(await screen.findByRole("button", { name: "OK" }));

    const accepted = vi
      .mocked(request)
      .mock.calls.find(
        ([path]) => path === `/v1/workflows/${WORKFLOW_ID}/execute`,
      );
    expect(accepted?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ inputs: { bid: "BID-42" } }),
    });
    expect(await screen.findByText(STARTED)).toBeTruthy();
    expect(onExecutionAccepted).toHaveBeenCalled();
    // The list is re-read, so the new run shows up without a manual refresh.
    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.filter(([path]) =>
            String(path).startsWith("/v1/executions?"),
          ),
      ).toHaveLength(2),
    );
  });

  it("blocks the re-run and names the missing piece instead of guessing", async () => {
    const user = userEvent.setup();
    renderPanel();

    const rerunButtons = await screen.findAllByRole("button", { name: RERUN });
    expect(rerunButtons[1]).toBeDisabled();

    const wrapper = rerunButtons[1].closest("span");
    await user.hover(wrapper ?? rerunButtons[1]);
    expect(await screen.findByText(MISSING_VERSION)).toBeTruthy();
  });
});
