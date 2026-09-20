/**
 * Definition validation failures must be readable one defect at a time.
 *
 * The compiler aggregates every defect of a check phase into one refusal, and
 * the API keeps them in ``error.details.diagnostics``; this mounts the editor
 * against such a refusal and asserts that all of them are listed, that the
 * location travels with each one, and that a code without a translation shows
 * the server's message instead of an invented hint.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options?.count !== undefined ? `${key}:${String(options.count)}` : key,
    i18n: {
      exists: (key: string) =>
        key === "workflowDiagnostics.WORKFLOW_REFERENCE_UNKNOWN",
    },
  }),
}));

vi.mock("../../../api/modules/workbuddyWorkflows", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    workbuddyWorkflowsApi: { validateDefinition: vi.fn() },
  };
});

import { workbuddyWorkflowsApi } from "../../../api/modules/workbuddyWorkflows";
import DefinitionEditorModal from "./DefinitionEditorModal";

const ENVELOPE = {
  error: {
    code: "WF_INVALID_SCHEMA",
    message: "reference inputs.missing is not a declared input",
    details: {
      compiler_code: "WORKFLOW_REFERENCE_UNKNOWN",
      stage: "references",
      diagnostics: [
        {
          code: "WORKFLOW_REFERENCE_UNKNOWN",
          message: "reference inputs.missing is not a declared input",
          path: "nodes.greet.config.input",
          node_id: "greet",
          hint_key: "workflowDiagnostics.WORKFLOW_REFERENCE_UNKNOWN",
        },
        {
          code: "WORKFLOW_SOMETHING_NEW",
          message: "the compiler refused something brand new",
          path: "edges[0]",
          hint_key: "workflowDiagnostics.WORKFLOW_SOMETHING_NEW",
        },
      ],
    },
  },
};

it("lists every diagnostic and only the hints that exist", async () => {
  vi.mocked(workbuddyWorkflowsApi.validateDefinition).mockRejectedValue(
    new Error(
      `Request failed: 422 Unprocessable Entity - ${JSON.stringify(ENVELOPE)}`,
    ),
  );

  render(
    <DefinitionEditorModal
      open
      target={{ mode: "create" }}
      onClose={() => {}}
      onSaved={() => {}}
      onStale={() => {}}
    />,
  );

  await userEvent.click(
    screen.getByRole("button", { name: "workbuddy.workflows.editor.validate" }),
  );

  expect(
    await screen.findByText("WORKFLOW_REFERENCE_UNKNOWN"),
  ).toBeInTheDocument();
  expect(screen.getByText("WORKFLOW_SOMETHING_NEW")).toBeInTheDocument();
  expect(screen.getByText("edges[0]")).toBeInTheDocument();
  expect(screen.getByText("nodes.greet.config.input")).toBeInTheDocument();
  expect(
    screen.getByText("the compiler refused something brand new"),
  ).toBeInTheDocument();
  expect(
    screen.getByText("workbuddy.workflows.editor.diagnosticCount:2"),
  ).toBeInTheDocument();
  // The known key renders its hint; the unknown one shows only the server text.
  expect(
    screen.getByText("workflowDiagnostics.WORKFLOW_REFERENCE_UNKNOWN"),
  ).toBeInTheDocument();
  expect(
    screen.queryByText("workflowDiagnostics.WORKFLOW_SOMETHING_NEW"),
  ).not.toBeInTheDocument();
});
