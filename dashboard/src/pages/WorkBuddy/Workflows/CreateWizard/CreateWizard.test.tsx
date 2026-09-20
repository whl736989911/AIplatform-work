/**
 * The guided creation wizard (A-06).
 *
 * Three promises are worth a test: the step form is built from the server's
 * metadata (not from a copy inside the component), a diagnostic that names the
 * current step blocks "next", and saving publishes through the workflow routes
 * in order.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const METADATA = {
  schema_version: 1,
  compiler_version: "test",
  node_types: [
    {
      type: "tool",
      required: ["tool_name", "parameters"],
      optional: [],
      config_fields: [
        { name: "tool_name", type: "string" },
        { name: "parameters", type: "object" },
      ],
      template_fields: ["parameters"],
    },
    {
      type: "output",
      required: ["value"],
      optional: [],
      config_fields: [{ name: "value", type: "string" }],
      template_fields: ["value"],
    },
  ],
  node: { required: ["id", "type", "name", "config"], optional: [], fields: [] },
  inputs: { required: [], optional: [], fields: [], types: ["string", "integer"] },
  edges: { required: [], optional: [], fields: [] },
  trigger_types: [],
  limits: { required: [], optional: [], fields: [] },
  output: { required: [], optional: [], fields: [] },
  reference_syntax: {
    identifier: { pattern: "^[a-z][a-z0-9_]{0,63}$" },
    template: { open: "{{", close: "}}", examples: [], max_placeholders: 32, fields: {} },
    references: [],
    max_reference_length: 64,
  },
  cel_reference_namespaces: [],
};

/** Every write the wizard performs, in order. */
const writes: string[] = [];
/** Set by a test that wants the server to refuse the current definition. */
let validateFailure: unknown = null;

const request = vi.fn(async (path: string, init?: { method?: string }) => {
  if (path === "/v1/workflow-definitions/metadata") return { data: METADATA };
  if (path === "/v1/workflow-definitions/validate") {
    if (validateFailure !== null) throw validateFailure;
    return { data: { valid: true } };
  }
  if ((init?.method ?? "GET") !== "GET") writes.push(path);
  if (path === "/v1/workflows") return { data: { id: "wf-1", revision: 1 } };
  if (path.startsWith("/v1/workflows/wf-1")) {
    return { data: { id: "wf-1", revision: 1, version: { id: "v-1" } } };
  }
  return { data: {} };
});

vi.mock("../../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../api/request")>();
  return {
    ...actual,
    request: (path: string, init?: { method?: string }) => request(path, init),
  };
});

import CreateWizard from "./index";

async function openStepTwo(user: UserEvent) {
  render(<CreateWizard open onClose={() => {}} onCreated={() => {}} />);
  await user.type(screen.getByLabelText(/workbuddy\.workflows\.wizard\.name/), "报销问答");
  await user.click(await screen.findByText("workbuddy.workflows.wizard.input.add"));
  await user.click(screen.getByText("workbuddy.workflows.wizard.next"));
}

describe("CreateWizard", () => {
  beforeEach(() => {
    writes.length = 0;
    validateFailure = null;
    request.mockClear();
  });

  it("builds the step form from the server's metadata", async () => {
    const user = userEvent.setup({ delay: null });
    await openStepTwo(user);
    await user.click(screen.getByText("workbuddy.workflows.wizard.type.tool"));

    // The two required config fields come from the metadata document, not from
    // a list compiled into this component.
    await waitFor(() => {
      expect(screen.getByText(/tool_name/)).toBeTruthy();
      expect(screen.getByText(/parameters/)).toBeTruthy();
    });
  });

  it("blocks the next step while the server reports a diagnostic for it", async () => {
    validateFailure = Object.assign(new Error("invalid"), {
      details: {
        diagnostics: [
          {
            code: "WORKFLOW_SCHEMA_INVALID",
            message: "config.tool_name is required",
            // The step the wizard just created is identified by its node id.
            path: "nodes.tool.config.tool_name",
            node_id: "tool",
          },
        ],
      },
    });
    const user = userEvent.setup({ delay: null });
    await openStepTwo(user);
    await user.click(screen.getByText("workbuddy.workflows.wizard.type.tool"));

    await waitFor(() => {
      expect(screen.getByText(/config\.tool_name is required/)).toBeTruthy();
    });
    const next = screen.getByText("workbuddy.workflows.wizard.next").closest("button");
    expect(next?.disabled).toBe(true);
  });

  it("publishes through create → save → activate", async () => {
    const user = userEvent.setup({ delay: null });
    await openStepTwo(user);
    await user.click(screen.getByText("workbuddy.workflows.wizard.type.tool"));
    await user.click(screen.getByText("workbuddy.workflows.wizard.next"));
    await user.click(screen.getByText("workbuddy.workflows.wizard.next"));
    await user.click(screen.getByText("workbuddy.workflows.wizard.save"));

    await waitFor(() => {
      expect(writes).toEqual([
        "/v1/workflows",
        "/v1/workflows/wf-1",
        "/v1/workflows/wf-1/activate",
      ]);
    });
  });
});
