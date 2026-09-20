/**
 * The inbox's "questions waiting on me" pane and the answer drawer.
 *
 * A question is asked by a run and answered in the drawer, so these cases drive
 * the two halves that must agree with the server: the list the pane renders
 * (rows, and an empty result that stays empty), and the answer the drawer
 * submits — built from the declared fields, with unanswered required fields kept
 * from the server entirely, and the server's own refusal shown when an answer
 * does reach it and is rejected.
 *
 * The controls are driven with ``fireEvent`` rather than typed character by
 * character: what these cases assert is the payload and the visible outcome,
 * and jsdom re-renders antd's form on every keystroke.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import type { InputRequest } from "../../../api/modules/workbuddyRuntime";
import InputInboxPanel from "./InputInboxPanel";

// i18n is auto-mocked in tests, so an un-translated key surfaces as its key.
const PANEL_TITLE = "workbuddy.inbox.inputs.title";
const EMPTY_TITLE = "workbuddy.inbox.inputs.emptyTitle";
const REQUIRED = "workbuddy.inbox.inputs.fieldRequired";
const SUBMIT = "workbuddy.inbox.inputs.submit";
const ANSWER = "workbuddy.inbox.inputs.answer";

const EXECUTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const LIST_PATH = "/v1/input-requests?scope=self&status=open&limit=100";
const EXECUTION_LIST_PATH = `/v1/executions/${EXECUTION_ID}/input-requests`;
const ANSWER_PATH = `${EXECUTION_LIST_PATH}/${REQUEST_ID}/answer`;

/** One question carrying a field of every kind the runtime accepts. */
const QUESTION: InputRequest = {
  id: REQUEST_ID,
  execution_id: EXECUTION_ID,
  node_id: "quote",
  status: "open",
  prompt: "How should we price the renewal?",
  form: {
    fields: [
      { name: "quote", label: "Quote", type: "number", required: true },
      { name: "seats", label: "Seats", type: "integer", required: true },
      { name: "note", label: "Note", type: "text", required: false },
      {
        name: "plan",
        label: "Plan",
        type: "select",
        required: false,
        options: ["pro", "enterprise"],
      },
      { name: "renew", label: "Renew", type: "boolean", required: true },
      { name: "start", label: "Start", type: "date", required: true },
      { name: "contact", label: "Contact", type: "string", required: true },
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

/** What request() throws when the server refuses an answer (422). */
function refusal(message: string): Error {
  return new Error(
    `Request failed: 422 Unprocessable Entity - ${JSON.stringify({
      error: { code: "WORKBUDDY_VALIDATION_FAILED", message },
    })}`,
  );
}

const envelope = (items: InputRequest[]) => ({
  data: { items },
  request_id: "test",
});

/** The execution the answer route returns once the run moved on. */
const EXECUTION_ENVELOPE = {
  data: {
    id: EXECUTION_ID,
    workflow_id: "44444444-4444-4444-8444-444444444444",
    workflow_version_id: "55555555-5555-4555-8555-555555555555",
    status: "running",
    trigger_type: "manual",
    inputs: {},
    outputs: {},
    error_code: null,
    error_message: null,
    created_by_user_id: 7,
    created_at: "2026-09-01T10:00:00+00:00",
    started_at: "2026-09-01T10:00:01+00:00",
    finished_at: null,
  },
  request_id: "test",
};

/** Answer the inbox list and the run's own list with the same questions. */
function serve(items: InputRequest[] = [QUESTION], refuse?: string) {
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (path === LIST_PATH) return envelope(items);
    if (path === EXECUTION_LIST_PATH) return envelope(items);
    if (path === ANSWER_PATH) {
      if (refuse) throw refusal(refuse);
      return EXECUTION_ENVELOPE;
    }
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
}

const callsTo = (path: string) =>
  vi.mocked(request).mock.calls.filter(([called]) => called === path);

beforeEach(() => {
  vi.mocked(request).mockClear();
  serve();
});

/** Render the pane and open the drawer for the one row it lists. */
async function openDrawer() {
  render(<InputInboxPanel />);
  fireEvent.click(await screen.findByRole("button", { name: ANSWER }));
  await screen.findByLabelText("Quote");
}

/** Set a control the way the browser reports it, for the field named. */
function setValue(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("input inbox pane", () => {
  it("renders the questions assigned to the caller and the run that asked", async () => {
    render(<InputInboxPanel />);

    expect(await screen.findByText(QUESTION.prompt)).toBeTruthy();
    expect(screen.getByText(PANEL_TITLE)).toBeTruthy();
    // The run a question came from, and the default "mine, still open" read.
    expect(screen.getByText("aaaaaaaa")).toBeTruthy();
    expect(callsTo(LIST_PATH)).toHaveLength(1);
  });

  it("stays empty instead of inventing a question", async () => {
    serve([]);
    render(<InputInboxPanel />);

    expect(await screen.findByText(EMPTY_TITLE)).toBeTruthy();
    expect(screen.queryByText(QUESTION.prompt)).toBeNull();
    expect(screen.queryByRole("button", { name: ANSWER })).toBeNull();
  });
});

describe("answer drawer", () => {
  it("submits the declared fields in the shape each type promises", async () => {
    serve([QUESTION], undefined);
    await openDrawer();

    setValue("Quote", "1200.5");
    setValue("Seats", "3");
    setValue("Note", "net 30");
    const plan = screen.getByLabelText("Plan");
    fireEvent.mouseDown(plan.closest(".ant-select-selector") ?? plan);
    fireEvent.click(await screen.findByTitle("pro"));
    fireEvent.click(screen.getByLabelText("Renew"));
    const start = screen.getByLabelText("Start");
    fireEvent.change(start, { target: { value: "2030-06-01" } });
    fireEvent.keyDown(start, { key: "Enter" });
    setValue("Contact", "D");
    fireEvent.click(screen.getByRole("button", { name: SUBMIT }));

    await waitFor(() => expect(callsTo(ANSWER_PATH)).toHaveLength(1));
    const [, init] = callsTo(ANSWER_PATH)[0];
    // Numbers stay numbers, the date is the ``YYYY-MM-DD`` its field declares,
    // and no key is sent that the form does not declare.
    expect(JSON.parse(String(init?.body))).toEqual({
      values: {
        quote: 1200.5,
        seats: 3,
        note: "net 30",
        plan: "pro",
        renew: true,
        start: "2030-06-01",
        contact: "D",
      },
    });
    // The answered question is gone from the pane it no longer belongs to.
    await waitFor(() => expect(callsTo(LIST_PATH)).toHaveLength(2));
    await waitFor(() => expect(screen.queryByLabelText("Note")).toBeNull());
  });

  it("keeps unanswered required fields from being submitted", async () => {
    await openDrawer();

    setValue("Contact", "D");
    fireEvent.click(screen.getByRole("button", { name: SUBMIT }));

    // Every unanswered required field is named under its control — the three
    // empty text controls, not the switch, which starts at the answer ``false``.
    await waitFor(() =>
      expect(
        document.querySelectorAll(".ant-form-item-explain-error"),
      ).toHaveLength(3),
    );
    expect(
      [...document.querySelectorAll(".ant-form-item-explain-error")].map(
        (node) => node.textContent,
      ),
    ).toEqual([REQUIRED, REQUIRED, REQUIRED]);
    expect(callsTo(ANSWER_PATH)).toHaveLength(0);
  });

  it("shows the server's own refusal of an answer it rejected", async () => {
    // A blank-only answer looks filled to the form but is an absence to the
    // server, which refuses by field name — that message is what is shown.
    const refusalMessage = "answer for field 'contact' is required";
    serve([QUESTION], refusalMessage);
    await openDrawer();

    setValue("Quote", "1200.5");
    setValue("Seats", "3");
    fireEvent.click(screen.getByLabelText("Renew"));
    const start = screen.getByLabelText("Start");
    fireEvent.change(start, { target: { value: "2030-06-01" } });
    fireEvent.keyDown(start, { key: "Enter" });
    setValue("Contact", " ");
    fireEvent.click(screen.getByRole("button", { name: SUBMIT }));

    expect(await screen.findByText(refusalMessage)).toBeTruthy();
    await waitFor(() => expect(callsTo(ANSWER_PATH)).toHaveLength(1));
    // The blank was left out of the payload — the server's own reason, verbatim.
    expect(JSON.parse(String(callsTo(ANSWER_PATH)[0][1]?.body))).toEqual({
      values: {
        quote: 1200.5,
        seats: 3,
        renew: true,
        start: "2030-06-01",
      },
    });
    // The drawer stays open with the operator's input, so the answer can be fixed.
    expect(screen.getByLabelText("Contact")).toHaveValue(" ");

    // Reopening the question starts clean instead of repeating the refusal.
    fireEvent.click(screen.getByLabelText("Close"));
    fireEvent.click(await screen.findByRole("button", { name: ANSWER }));
    await screen.findByLabelText("Quote");
    expect(screen.queryByText(refusalMessage)).toBeNull();
  });
});
