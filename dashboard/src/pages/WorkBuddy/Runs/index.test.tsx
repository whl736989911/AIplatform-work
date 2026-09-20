/**
 * Runs list → the entry for a run parked on a question.
 *
 * A run waiting on an ``ask`` node is the fourth inbox entry point, and the
 * only one reached from the runs list: it must offer the answer drawer for that
 * run and for no other row, and the drawer has to read the run's own questions
 * rather than guess at one.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
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
import type {
  Execution,
  InputRequest,
} from "../../../api/modules/workbuddyRuntime";
import RunsPage from "./index";

const ANSWER = "workbuddy.runs.answer";

const WORKFLOW_ID = "44444444-4444-4444-8444-444444444444";
const WAITING_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function execution(id: string, status: Execution["status"]): Execution {
  return {
    id,
    workflow_id: WORKFLOW_ID,
    workflow_version_id: "55555555-5555-4555-8555-555555555555",
    status,
    trigger_type: "manual",
    inputs: {},
    outputs: {},
    error_code: null,
    error_message: null,
    created_by_user_id: 7,
    created_at: "2026-09-01T10:00:00+00:00",
    started_at: "2026-09-01T10:00:01+00:00",
    finished_at: null,
  };
}

/** The question that parked ``WAITING_ID``. */
const QUESTION: InputRequest = {
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  execution_id: WAITING_ID,
  node_id: "quote",
  status: "open",
  prompt: "How should we price the renewal?",
  form: {
    fields: [
      { name: "quote", label: "Quote", type: "number", required: true },
    ],
  },
  values: null,
  expires_at: "2030-01-01T00:00:00+00:00",
  submitted_by_user_id: null,
  submitted_at: null,
  created_at: "2026-09-01T10:00:00+00:00",
  assignees: [
    { user_id: 7, department_id: null, status: "pending", submitted_at: null },
  ],
};

const ENVELOPE = (data: unknown) => ({ data, request_id: "test" });

beforeEach(() => {
  vi.mocked(request).mockClear();
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (String(path).startsWith("/v1/executions?")) {
      return ENVELOPE({
        items: [
          execution(WAITING_ID, "waiting_input"),
          execution("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "running"),
        ],
      });
    }
    if (path === `/v1/executions/${WAITING_ID}/input-requests`) {
      return ENVELOPE({ items: [QUESTION] });
    }
    // The workflow name lookup and everything else is not served here.
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
});

describe("runs list → waiting on input", () => {
  it("offers the answer drawer for the run parked on a question only", async () => {
    render(
      <MemoryRouter initialEntries={["/workbuddy/runs"]}>
        <RunsPage />
      </MemoryRouter>,
    );

    const answerButtons = await screen.findAllByRole("button", {
      name: ANSWER,
    });
    expect(answerButtons).toHaveLength(1);

    await userEvent.setup().click(answerButtons[0]);

    // The drawer reads that run's own questions and renders their form.
    expect(await screen.findByText(QUESTION.prompt)).toBeTruthy();
    expect(
      await screen.findByRole("button", {
        name: "workbuddy.inbox.inputs.submit",
      }),
    ).toBeTruthy();
    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.some(
            ([path]) => path === `/v1/executions/${WAITING_ID}/input-requests`,
          ),
      ).toBe(true),
    );
  });
});
