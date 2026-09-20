/**
 * Describing a workflow and getting a draft back (A-15).
 *
 * The dialog's job is small and worth pinning: it sends a description, it shows
 * what the author said each step is for, and — the part that makes the feature
 * honest — when the compiler refuses, the person sees the compiler's own
 * diagnostics rather than a shrug. It never publishes anything.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const AUTHORED = {
  id: "wf-9",
  name: "客户邮件回复",
  revision: 1,
  active_version_id: null,
  version: { id: "v-1" },
  authoring: {
    rounds: 2,
    steps: [
      { id: "extract", kind: "llm", purpose: "从邮件里抽出关键信息", uses: [] },
      { id: "reply", kind: "llm", purpose: "起草一封回复", uses: ["extract"] },
    ],
  },
};

const REFUSAL = new Error(
  JSON.stringify({
    error: {
      code: "WORKBUDDY_VALIDATION_FAILED",
      message: "the description did not produce a compilable workflow",
      details: {
        rounds: 2,
        diagnostics: [
          {
            code: "WORKFLOW_SCHEMA_INVALID",
            message: "'cron_expression' is a required property",
            path: "trigger.config",
            node_id: null,
          },
        ],
      },
    },
  }),
);

const calls: Array<{ path: string; body: unknown }> = [];
let failure: unknown = null;

const request = vi.fn(async (path: string, init?: { body?: unknown }) => {
  calls.push({ path, body: init?.body });
  if (failure !== null) throw failure;
  return { data: AUTHORED };
});

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("../../../api/request")
  >();
  return {
    ...actual,
    request: (path: string, init?: { body?: unknown }) => request(path, init),
  };
});

import AuthorWorkflowModal from "./AuthorWorkflowModal";

beforeEach(() => {
  calls.length = 0;
  failure = null;
  request.mockClear();
});

async function describeIt(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
) {
  await user.type(
    screen.getByPlaceholderText(/workbuddy\.workflows\.author\.placeholder/),
    text,
  );
  // The generate button is disabled until the description has content, so the
  // click must wait for that render or it lands on a disabled button.
  const generate = screen.getByRole("button", {
    name: /workbuddy\.workflows\.author\.generate/,
  }) as HTMLButtonElement;
  await waitFor(() => expect(generate.disabled).toBe(false));
  await user.click(generate);
}

describe("AuthorWorkflowModal", () => {
  it("sends the description and explains the steps it got back", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <AuthorWorkflowModal open onClose={() => {}} onCreated={onCreate} />,
    );

    await describeIt(user, "客户来信后先抽取信息，再起草回复");

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].path).toBe("/v1/workflow-authoring");
    // The description travels as written; no definition is ever built here.
    expect(JSON.parse(String(calls[0].body))).toEqual({
      request: "客户来信后先抽取信息，再起草回复",
    });

    // The author's own words per step, and how many rounds it took.
    expect(await screen.findByText("从邮件里抽出关键信息")).toBeTruthy();
    expect(screen.getByText("起草一封回复")).toBeTruthy();
    expect(
      screen.getByText(/workbuddy\.workflows\.author\.rounds/),
    ).toBeTruthy();

    await user.click(
      screen.getByRole("button", {
        name: /workbuddy\.workflows\.author\.openDraft/,
      }),
    );
    expect(onCreate).toHaveBeenCalledWith("wf-9");
  });

  it("shows the compiler's diagnostics when the description does not compile", async () => {
    failure = REFUSAL;
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <AuthorWorkflowModal open onClose={() => {}} onCreated={onCreate} />,
    );

    await describeIt(user, "写不出来的东西");

    // What a person needs is the compiler's complaint, not a generic failure.
    expect(await screen.findByText("WORKFLOW_SCHEMA_INVALID")).toBeTruthy();
    expect(
      screen.getByText("'cron_expression' is a required property"),
    ).toBeTruthy();
    expect(screen.getByText("trigger.config")).toBeTruthy();
    // Nothing was created, so there is nothing to open.
    expect(
      screen.queryByRole("button", {
        name: /workbuddy\.workflows\.author\.openDraft/,
      }),
    ).toBeNull();
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("will not send an empty description", async () => {
    const user = userEvent.setup();
    render(
      <AuthorWorkflowModal open onClose={() => {}} onCreated={() => {}} />,
    );
    const generate = screen.getByRole("button", {
      name: /workbuddy\.workflows\.author\.generate/,
    });
    expect((generate as HTMLButtonElement).disabled).toBe(true);
    await user.type(
      screen.getByPlaceholderText(/workbuddy\.workflows\.author\.placeholder/),
      "x",
    );
    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: /workbuddy\.workflows\.author\.generate/,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
    expect(calls).toHaveLength(0);
  });
});
