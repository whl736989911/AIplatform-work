/**
 * The inbox's "outputs waiting on my review" pane and the review drawer.
 *
 * A review is about a run that already settled, and these cases drive the two
 * halves that must agree with the server: the queue the pane renders (rows, and
 * an empty result that stays empty), and what the drawer submits — only the keys
 * a reviewer changed, only the keys the run produced (there is nowhere to name a
 * new one), the bodies ``accept`` and ``rerun`` are defined by, and the server's
 * own refusal shown verbatim when it rejects a decision.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  OutputReview,
} from "../../../api/modules/workbuddyRuntime";
import OutputReviewPanel from "./OutputReviewPanel";

// i18n is auto-mocked in tests, so an un-translated key surfaces as its key.
const PANEL_TITLE = "workbuddy.inbox.reviews.title";
const EMPTY_TITLE = "workbuddy.inbox.reviews.emptyTitle";
const REVIEW = "workbuddy.inbox.reviews.review";
const ACCEPT = "workbuddy.inbox.reviews.accept";
const CORRECT = "workbuddy.inbox.reviews.correct";
const RERUN = "workbuddy.inbox.reviews.rerun";
const RERUN_CONFIRM = "workbuddy.inbox.reviews.rerunConfirm";
const RERUN_INPUTS = "workbuddy.inbox.reviews.rerunInputs";

const EXECUTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const REVIEW_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const RERUN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

const LIST_PATH = "/v1/output-reviews?scope=self&status=open&limit=100";
const REVIEW_PATH = `/v1/executions/${EXECUTION_ID}/output-review`;
const DECISION_PATH = `${REVIEW_PATH}/decisions`;
const EXECUTION_PATH = `/v1/executions/${EXECUTION_ID}`;

/** One open review of a settled run, with a value of each shape under review. */
const REVIEW_ROW: OutputReview = {
  id: REVIEW_ID,
  execution_id: EXECUTION_ID,
  status: "open",
  produced: { total: 1200, note: "net 30" },
  produced_sha256: "9f2c4d1e0b7a3f5c8d6e4b2a0f9e7d5c3b1a8f6e4d2c0b9a7f5e3d1c",
  corrected: null,
  requested_by_user_id: 7,
  decided_by_user_id: null,
  decided_at: null,
  rerun_execution_id: null,
  created_at: "2026-09-01T10:00:00+00:00",
  reviewers: [
    { user_id: 7, department_id: null, status: "pending", decided_at: null },
  ],
};

/** The run the review is about: settled, with the inputs a re-run would reuse. */
const EXECUTION: Execution = {
  id: EXECUTION_ID,
  workflow_id: "44444444-4444-4444-8444-444444444444",
  workflow_version_id: "55555555-5555-4555-8555-555555555555",
  status: "success",
  trigger_type: "manual",
  inputs: { order: "A-1" },
  outputs: { total: 1200, note: "net 30" },
  error_code: null,
  error_message: null,
  created_by_user_id: 7,
  created_at: "2026-09-01T10:00:00+00:00",
  started_at: "2026-09-01T10:00:01+00:00",
  finished_at: "2026-09-01T10:00:09+00:00",
};

const envelope = (data: unknown) => ({ data, request_id: "test" });

/** What request() throws when the server refuses a decision. */
function refusal(status: string, code: string, message: string): Error {
  return new Error(
    `Request failed: ${status} - ${JSON.stringify({ error: { code, message } })}`,
  );
}

/** Serve the queue, one review and the decision route the drawer calls. */
function serve({
  items = [REVIEW_ROW],
  decided = { ...REVIEW_ROW, status: "accepted" as const },
  refuse,
}: {
  items?: OutputReview[];
  decided?: OutputReview;
  refuse?: Error;
} = {}) {
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (path === LIST_PATH) return envelope({ items });
    if (path === REVIEW_PATH) return envelope(REVIEW_ROW);
    if (path === EXECUTION_PATH) return envelope(EXECUTION);
    if (path === DECISION_PATH) {
      if (refuse) throw refuse;
      return envelope(decided);
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

function renderPanel() {
  return render(
    <MemoryRouter>
      <OutputReviewPanel />
    </MemoryRouter>,
  );
}

/** Render the pane and open the review drawer for the one row it lists. */
async function openDrawer() {
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: REVIEW }));
  await screen.findByLabelText("total");
}

describe("output review pane", () => {
  it("lists the reviews the caller was asked to do", async () => {
    renderPanel();

    expect(await screen.findByText(PANEL_TITLE)).toBeTruthy();
    // The self scope means "asked of me", and the default read is the open queue.
    expect(
      screen.getByText("workbuddy.inbox.reviews.assignedToMe"),
    ).toBeTruthy();
    // The row's own status tag — the filter above it is a different "open".
    expect(within(screen.getByRole("table")).getByText("open")).toBeTruthy();
    expect(screen.getByText("aaaaaaaa")).toBeTruthy();
    // What is under review is the run's own output, key by key.
    expect(screen.getByText("total")).toBeTruthy();
    expect(screen.getByText("note")).toBeTruthy();
    expect(callsTo(LIST_PATH)).toHaveLength(1);
  });

  it("stays empty instead of inventing a review", async () => {
    serve({ items: [] });
    renderPanel();

    expect(await screen.findByText(EMPTY_TITLE)).toBeTruthy();
    expect(screen.queryByText("total")).toBeNull();
    expect(screen.queryByRole("button", { name: REVIEW })).toBeNull();
  });
});

describe("review drawer", () => {
  it("shows who was asked, and one control per produced key", async () => {
    await openDrawer();

    // The roster the queue route does not carry travels with the review itself.
    const roster = within(
      screen.getByText("workbuddy.inbox.reviews.reviewers")
        .closest("div") as HTMLElement,
    );
    expect(roster.getByText("#7")).toBeTruthy();
    expect(roster.getByText("pending")).toBeTruthy();

    // Two produced keys, two controls — and no third input an invented key
    // could be typed into.
    expect(screen.getAllByRole("textbox")).toHaveLength(2);
    expect(screen.getByLabelText("total")).toHaveValue("1200");
    expect(screen.getByLabelText("note")).toHaveValue('"net 30"');
  });

  it("submits a correction with only the keys that changed", async () => {
    await openDrawer();

    // Nothing has changed, so there is nothing to confirm yet.
    expect(screen.getByRole("button", { name: CORRECT })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("note"), {
      target: { value: '"net 45"' },
    });
    fireEvent.click(screen.getByRole("button", { name: CORRECT }));

    await waitFor(() => expect(callsTo(DECISION_PATH)).toHaveLength(1));
    expect(JSON.parse(String(callsTo(DECISION_PATH)[0][1]?.body))).toEqual({
      decision: "correct",
      corrected: { note: "net 45" },
    });
    // The decision the server recorded is what the drawer shows now, and the
    // queue this review left is refetched.
    expect(await screen.findByText("accepted")).toBeTruthy();
    await waitFor(() => expect(callsTo(LIST_PATH)).toHaveLength(2));
  });

  it("treats a reformatted value as no change at all", async () => {
    await openDrawer();

    // Same value, one line instead of two: nothing was corrected.
    fireEvent.change(screen.getByLabelText("total"), {
      target: { value: "1200" },
    });
    expect(screen.getByRole("button", { name: CORRECT })).toBeDisabled();
  });

  it("accepts the output as produced", async () => {
    await openDrawer();

    fireEvent.click(screen.getByRole("button", { name: ACCEPT }));

    await waitFor(() => expect(callsTo(DECISION_PATH)).toHaveLength(1));
    expect(JSON.parse(String(callsTo(DECISION_PATH)[0][1]?.body))).toEqual({
      decision: "accept",
    });
  });

  it("re-runs with the run's own inputs, editable before it starts", async () => {
    serve({
      decided: {
        ...REVIEW_ROW,
        status: "rerun",
        rerun_execution_id: RERUN_ID,
      },
    });
    await openDrawer();

    fireEvent.click(screen.getByRole("button", { name: RERUN }));
    const inputs = await screen.findByLabelText(RERUN_INPUTS);
    // Prefilled from the reviewed run, which is what a re-run would reuse.
    await waitFor(() =>
      expect(inputs).toHaveValue(JSON.stringify(EXECUTION.inputs, null, 2)),
    );
    fireEvent.change(inputs, { target: { value: '{"order":"A-2"}' } });
    fireEvent.click(screen.getByRole("button", { name: RERUN_CONFIRM }));

    await waitFor(() => expect(callsTo(DECISION_PATH)).toHaveLength(1));
    expect(JSON.parse(String(callsTo(DECISION_PATH)[0][1]?.body))).toEqual({
      decision: "rerun",
      inputs: { order: "A-2" },
    });
    // The review now points at the execution the re-run started.
    expect(await screen.findByText(RERUN_ID.slice(0, 8))).toBeTruthy();
  });

  it("re-runs without inputs it could not read, instead of inventing an empty set", async () => {
    // The reviewed run's own inputs are unreadable, which is exactly when a
    // client must not send `{}`: the server replays what it recorded instead.
    vi.mocked(request).mockImplementation((async (path: string) => {
      if (path === LIST_PATH) return envelope({ items: [REVIEW_ROW] });
      if (path === REVIEW_PATH) return envelope(REVIEW_ROW);
      if (path === DECISION_PATH) return envelope(REVIEW_ROW);
      throw new Error('Request failed: 500 Server Error - {"detail":"boom"}');
    }) as typeof request);
    await openDrawer();

    fireEvent.click(screen.getByRole("button", { name: RERUN }));
    expect(
      await screen.findByText("workbuddy.inbox.reviews.rerunInputsUnread"),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: RERUN_CONFIRM }));

    await waitFor(() => expect(callsTo(DECISION_PATH)).toHaveLength(1));
    expect(JSON.parse(String(callsTo(DECISION_PATH)[0][1]?.body))).toEqual({
      decision: "rerun",
    });
  });

  it("shows the server's own refusal of a decision it already settled", async () => {
    const conflict = "this review has already been decided";
    serve({ refuse: refusal("409 Conflict", "STATE_CONFLICT", conflict) });
    await openDrawer();

    fireEvent.click(screen.getByRole("button", { name: ACCEPT }));

    expect(await screen.findByText(conflict)).toBeTruthy();
    // Refused means refused: the drawer stays open on the review as it is.
    expect(screen.getByRole("button", { name: ACCEPT })).toBeTruthy();
  });

  it("shows the server's own refusal of a correction it rejected", async () => {
    const rejected = "the run produced no output named 'margin'";
    serve({
      refuse: refusal("400 Bad Request", "WORKBUDDY_VALIDATION_FAILED", rejected),
    });
    await openDrawer();

    fireEvent.change(screen.getByLabelText("note"), {
      target: { value: '"net 45"' },
    });
    fireEvent.click(screen.getByRole("button", { name: CORRECT }));

    expect(await screen.findByText(rejected)).toBeTruthy();
  });
});
