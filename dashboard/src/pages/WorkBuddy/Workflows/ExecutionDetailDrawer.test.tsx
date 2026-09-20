/**
 * Per-node triage in the execution drawer.
 *
 * The drawer is the only surface where an operator can see what one node was
 * handed, what it produced, how long it took and what it cost, so this mounts it
 * against a detail payload that carries those diagnostics and asserts they reach
 * the page — and that a payload *without* them (the shape a deployment that
 * predates the diagnostics sends) renders dashes instead of invented values.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  // Exactly what request() throws for a route this test does not answer.
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
import type { ExecutionDetail } from "../../../api/modules/workbuddyRuntime";
import ExecutionDetailDrawer from "./ExecutionDetailDrawer";

// These specs drive antd Drawer/Select/Table inside jsdom; the default 5s
// per-test budget is measured against a full interaction chain and is too tight
// on a 2-core CI runner, where a timeout reads as "the drawer is broken".
vi.setConfig({ testTimeout: 20_000 });

const EXECUTION_ID = "11111111-1111-4111-8111-111111111111";
const WORKFLOW_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";

/** The payload the server returns once the step diagnostics are wired. */
const DIAGNOSED: ExecutionDetail = {
  id: EXECUTION_ID,
  workflow_id: WORKFLOW_ID,
  workflow_version_id: VERSION_ID,
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
  active_duration_ms: 6400,
  token_usage: 4096,
  steps: [
    {
      node_id: "classify",
      node_type: "llm",
      attempt: 2,
      status: "failed",
      save_as: "verdict",
      error_code: "TOOL_TIMEOUT",
      input: {
        inputs: { bid: "BID-42" },
        outputs: {},
        input: { text: "hello" },
        node: { id: "classify" },
        workflow: { id: WORKFLOW_ID },
      },
      output: { verdict: "needs_review" },
      duration_ms: 1500,
      started_at: "2026-09-01T10:00:02+00:00",
      finished_at: "2026-09-01T10:00:03+00:00",
      skip_reason: null,
      token_usage: { total_tokens: 128, input_tokens: 100, output_tokens: 28 },
    },
    {
      node_id: "notify",
      node_type: "tool",
      attempt: 1,
      status: "skipped",
      save_as: null,
      error_code: null,
      input: null,
      output: null,
      duration_ms: null,
      started_at: null,
      finished_at: null,
      skip_reason: "upstream_failed",
      token_usage: null,
    },
  ],
  edges: [],
};

/** The same route answered by a deployment that records none of the diagnostics. */
const WITHOUT_DIAGNOSTICS: ExecutionDetail = {
  id: EXECUTION_ID,
  workflow_id: WORKFLOW_ID,
  workflow_version_id: VERSION_ID,
  status: "success",
  trigger_type: "manual",
  inputs: {},
  outputs: {},
  error_code: null,
  error_message: null,
  created_by_user_id: null,
  created_at: null,
  started_at: null,
  finished_at: null,
  steps: [
    {
      node_id: "summarise",
      node_type: "llm",
      attempt: 1,
      status: "success",
      save_as: null,
      error_code: null,
    },
  ],
  edges: [],
};

/** A run parked on an unknown external write: the one state a decision applies to. */
const PARKED: ExecutionDetail = {
  ...WITHOUT_DIAGNOSTICS,
  status: "waiting_reconciliation",
  steps: [
    {
      node_id: "submit",
      node_type: "tool",
      attempt: 1,
      status: "waiting_reconciliation",
      save_as: null,
      error_code: null,
      input: { node: { id: "submit" }, input: { bid: "BID-77" } },
      output: null,
      duration_ms: null,
      started_at: "2026-09-01T10:00:02+00:00",
      finished_at: null,
      skip_reason: null,
      token_usage: null,
    },
    {
      node_id: "after",
      node_type: "transform",
      attempt: 1,
      status: "queued",
      save_as: null,
      error_code: null,
    },
  ],
  edges: [],
};

let detail: ExecutionDetail = DIAGNOSED;

/** The payload ids the drawer exchanges with the record route. */
const EVIDENCE_REF = "99999999-9999-4999-8999-999999999999";
const STEP_RUN_ID = "88888888-8888-4888-8888-888888888888";

beforeEach(() => {
  detail = DIAGNOSED;
  // Vitest keeps `mock.calls` per file unless it is cleared, and one case here
  // asserts that a route was *not* called.
  vi.mocked(request).mockClear();
  vi.mocked(request).mockImplementation((async (
    path: string,
    init?: RequestInit,
  ) => {
    if (path === `/v1/executions/${EXECUTION_ID}`) {
      return { data: detail, request_id: "test" };
    }
    if (path.endsWith("/reconciliations")) {
      if (init?.method === "POST") {
        return {
          data: {
            id: "77777777-7777-4777-8777-777777777777",
            execution_id: EXECUTION_ID,
            node_id: "submit",
            step_run_id: STEP_RUN_ID,
            decision: "confirmed_success",
            external_reference: null,
            evidence_ref: EVIDENCE_REF,
            evidence_hash: "sha256:evidence",
            result_payload_ref: null,
            reconciliations: 1,
            execution: PARKED,
          },
          request_id: "test",
        };
      }
      return { data: { items: [] }, request_id: "test" };
    }
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
});

function renderDrawer() {
  render(<ExecutionDetailDrawer executionId={EXECUTION_ID} onClose={() => {}} />);
}

/** The rendered JSON blocks whose text carries this fragment. */
function jsonBlocks(fragment: string): HTMLElement[] {
  return screen.queryAllByText(
    (_content: string, element: Element | null) =>
      element?.tagName === "PRE" &&
      element.textContent?.includes(fragment) === true,
  );
}

describe("execution detail drawer", () => {
  it("shows what each node was handed, produced, cost and how long it took", async () => {
    const user = userEvent.setup();
    renderDrawer();

    // Both steps are listed; the diagnostics stay behind the row itself.
    expect(await screen.findByText("classify")).toBeTruthy();
    expect(screen.getByText("notify")).toBeTruthy();
    expect(jsonBlocks('"id": "classify"')).toHaveLength(0);

    await user.click(screen.getAllByRole("button", { name: "Expand row" })[0]);

    // The activation this node was handed, and the payload it recorded.
    expect(jsonBlocks('"id": "classify"')).toHaveLength(1);
    expect(jsonBlocks('"verdict": "needs_review"')).toHaveLength(1);

    // Duration is rendered in human units, for the step and for the run.
    expect(screen.getByText("1.5s")).toBeTruthy();
    expect(screen.getByText("6.4s")).toBeTruthy();

    // Adapter usage verbatim, and the run's own total.
    expect(screen.getByText("128")).toBeTruthy();
    expect(screen.getByText("100")).toBeTruthy();
    expect(screen.getByText("28")).toBeTruthy();
    expect(screen.getByText("4096")).toBeTruthy();

    await user.click(screen.getAllByRole("button", { name: "Expand row" })[0]);

    // A skipped node says why it produced nothing.
    expect(screen.getByText("upstream_failed")).toBeTruthy();
  });

  it("renders dashes instead of inventing a payload the server did not send", async () => {
    const user = userEvent.setup();
    detail = WITHOUT_DIAGNOSTICS;
    renderDrawer();

    expect(await screen.findByText("summarise")).toBeTruthy();
    await user.click(screen.getAllByRole("button", { name: "Expand row" })[0]);

    const durationCell = screen
      .getAllByText("workbuddy.workflows.detail.stepDuration")[0]
      .closest("th")?.nextElementSibling;
    expect(durationCell?.textContent).toBe("—");
    // Neither the step's input nor its output was recorded: both say so.
    expect(
      screen.getAllByText("workbuddy.workflows.detail.noPayload"),
    ).toHaveLength(2);
    const totalCell = screen
      .getAllByText("workbuddy.workflows.detail.tokensTotal")[0]
      .closest("th")?.nextElementSibling;
    expect(totalCell?.textContent).toBe("—");
    // The step reported no adapter usage at all, and says so instead of zeros.
    expect(
      screen.getByText("workbuddy.workflows.detail.tokenUsage").closest("div")
        ?.textContent,
    ).toBe("workbuddy.workflows.detail.tokenUsage—");
  });
});

describe("reconciliation decision", () => {
  it("submits the decision with the field names the route accepts", async () => {
    const user = userEvent.setup();
    detail = PARKED;
    renderDrawer();

    await user.click(
      await screen.findByRole("button", {
        name: "workbuddy.workflows.detail.record",
      }),
    );

    // The decision offers exactly the two outcomes the route accepts; with i18n
    // mocked, `statusLabel` falls back to the server's own words.
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByTitle("confirmed_success"));
    await user.type(
      screen.getByLabelText("workbuddy.workflows.detail.reconcileEvidenceRef"),
      EVIDENCE_REF,
    );
    await user.type(
      screen.getByLabelText("workbuddy.workflows.detail.reconcileReason"),
      "the provider lookup confirms the bid was accepted",
    );
    await user.click(
      screen.getByRole("button", {
        name: "workbuddy.workflows.detail.reconcileSubmit",
      }),
    );

    await waitFor(() =>
      expect(
        vi.mocked(request).mock.calls.some(([path, init]) => {
          if (path !== `/v1/executions/${EXECUTION_ID}/reconciliations`) {
            return false;
          }
          return (
            init?.method === "POST" &&
            init.body ===
              JSON.stringify({
                step_id: "submit",
                decision: "confirmed_success",
                evidence_ref: EVIDENCE_REF,
                reason: "the provider lookup confirms the bid was accepted",
                external_reference: null,
              })
          );
        }),
      ).toBe(true),
    );
  }, 20_000);

  it("refuses to submit a decision without the operator's reason", async () => {
    const user = userEvent.setup();
    detail = PARKED;
    renderDrawer();

    await user.click(
      await screen.findByRole("button", {
        name: "workbuddy.workflows.detail.record",
      }),
    );
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByTitle("confirmed_failed"));
    await user.type(
      screen.getByLabelText("workbuddy.workflows.detail.reconcileEvidenceRef"),
      EVIDENCE_REF,
    );
    await user.click(
      screen.getByRole("button", {
        name: "workbuddy.workflows.detail.reconcileSubmit",
      }),
    );

    // The server rejects a reasonless decision, so the form never sends one.
    expect(
      await screen.findByText("workbuddy.workflows.detail.reasonRequired"),
    ).toBeTruthy();
    expect(
      vi.mocked(request).mock.calls.some(
        ([path, init]) =>
          init?.method === "POST" &&
          String(path).endsWith(
            `/executions/${EXECUTION_ID}/reconciliations`,
          ),
      ),
    ).toBe(false);
  }, 20_000);
});
