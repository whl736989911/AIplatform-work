/**
 * Runs list → the two entries a row opens for a person.
 *
 * A run parked on a question offers the answer drawer for that run and for no
 * other row — the run's only way forward — while a settled run offers the review
 * entry, which asks the server whether a review exists instead of guessing and
 * asks for one when it does not.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  OutputReview,
} from "../../../api/modules/workbuddyRuntime";
import RunsPage from "./index";

const ANSWER = "workbuddy.runs.answer";
const REVIEW = "workbuddy.runs.review";
const REQUEST_REVIEW = "workbuddy.runs.reviewRequest";
const REQUEST_SUBMIT = "workbuddy.runs.reviewRequestSubmit";
const REQUEST_TITLE = "workbuddy.runs.reviewRequestTitle";

const WORKFLOW_ID = "44444444-4444-4444-8444-444444444444";
const WAITING_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SETTLED_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const MEMBERSHIP_ID = "99999999-9999-4999-8999-999999999999";

const REVIEW_PATH = `/v1/executions/${SETTLED_ID}/output-review`;

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

/** The review the request route answers with, as the console reads it. */
const CREATED_REVIEW: OutputReview = {
  id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  execution_id: SETTLED_ID,
  status: "open",
  produced: { greeting: "hello world" },
  produced_sha256: "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809",
  corrected: null,
  requested_by_user_id: 7,
  decided_by_user_id: null,
  decided_at: null,
  rerun_execution_id: null,
  created_at: "2026-09-02T10:00:00+00:00",
  reviewers: [
    { user_id: 9, department_id: null, status: "pending", decided_at: null },
  ],
};

const callsTo = (path: string) =>
  vi.mocked(request).mock.calls.filter(([called]) => called === path);

describe("runs list → output review", () => {
  it("offers a settlement review only for a settled run, then reads it back", async () => {
    // A small fake for the review route: 404 until the request lands, then the
    // review it created.
    let review: OutputReview | null = null;
    vi.mocked(request).mockImplementation((async (
      path: string,
      init?: RequestInit,
    ) => {
      if (String(path).startsWith("/v1/executions?")) {
        return ENVELOPE({
          items: [
            execution(SETTLED_ID, "success"),
            execution(WAITING_ID, "waiting_input"),
          ],
        });
      }
      if (path === REVIEW_PATH) {
        if (init?.method === "POST") {
          review = CREATED_REVIEW;
          return ENVELOPE(review);
        }
        if (review) return ENVELOPE(review);
      }
      throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
    }) as typeof request);

    render(
      <MemoryRouter initialEntries={["/workbuddy/runs"]}>
        <RunsPage />
      </MemoryRouter>,
    );

    // A run still moving has nothing to review; only the settled one does.
    const reviewButtons = await screen.findAllByRole("button", {
      name: REVIEW,
    });
    expect(reviewButtons).toHaveLength(1);
    await userEvent.setup().click(reviewButtons[0]);

    // No review exists yet, so the entry offers to ask for one.
    expect(callsTo(REVIEW_PATH)).toHaveLength(1);
    await userEvent.setup().click(
      await screen.findByRole("button", { name: REQUEST_REVIEW }),
    );

    const picker = within(
      screen.getByText(REQUEST_TITLE).closest(".ant-modal") as HTMLElement,
    ).getByRole("combobox");
    fireEvent.change(picker, { target: { value: `${MEMBERSHIP_ID},` } });
    await userEvent.setup().click(
      screen.getByRole("button", { name: REQUEST_SUBMIT }),
    );

    await waitFor(() => expect(callsTo(REVIEW_PATH)).toHaveLength(2));
    const posted = callsTo(REVIEW_PATH).filter(
      ([, init]) => init?.method === "POST",
    );
    expect(posted).toHaveLength(1);
    expect(JSON.parse(String(posted[0][1]?.body))).toEqual({
      reviewer_user_ids: [MEMBERSHIP_ID],
    });

    // The entry reads the route again and shows the review it just asked for,
    // and the list behind it is refreshed too.
    expect(await screen.findByText("open")).toBeTruthy();
    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.filter(([path]) =>
            String(path).startsWith("/v1/executions?"),
          ).length,
      ).toBe(2),
    );
  });

  it("opens the run named in the URL, so a re-run a review points at is linkable", async () => {
    const detailPath = `/v1/executions/${SETTLED_ID}`;
    vi.mocked(request).mockImplementation((async (path: string) => {
      if (String(path).startsWith("/v1/executions?")) {
        return ENVELOPE({ items: [execution(SETTLED_ID, "success")] });
      }
      if (path === detailPath) {
        return ENVELOPE({
          ...execution(SETTLED_ID, "success"),
          steps: [],
          edges: [],
        });
      }
      throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
    }) as typeof request);

    render(
      <MemoryRouter initialEntries={[`/workbuddy/runs?execution=${SETTLED_ID}`]}>
        <RunsPage />
      </MemoryRouter>,
    );

    // The detail drawer is open for that run without a click.
    await waitFor(() => expect(callsTo(detailPath)).toHaveLength(1));
  });
});
