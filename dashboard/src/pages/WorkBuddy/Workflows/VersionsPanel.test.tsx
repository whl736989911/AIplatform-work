/**
 * Version comparison (definition-level diff).
 *
 * The versions tab compares exactly two selected versions. This mounts the
 * panel against a version list and the diff route, and asserts the request the
 * panel actually sends (path plus ``from``/``to``, the older version first
 * whatever order the rows were ticked in) and that the response's changes reach
 * the view as path + kind. The other two cases pin the honesty of the states
 * around it: an empty change list is a real answer ("no differences"), while a
 * response that carried no list at all is reported as missing instead of being
 * dressed up as one.
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
import type { WorkflowVersion } from "../../../api/modules/workbuddyWorkflows";
import VersionsPanel from "./VersionsPanel";

const WORKFLOW_ID = "44444444-4444-4444-8444-444444444444";
const V1_ID = "11111111-1111-4111-8111-111111111111";
const V2_ID = "22222222-2222-4222-8222-222222222222";
const VERSIONS_PATH = `/v1/workflows/${WORKFLOW_ID}/versions`;
const DIFF_PATH = `${VERSIONS_PATH}/diff`;

// i18n is auto-mocked in tests, so an un-translated key surfaces as its key.
const COMPARE = "workbuddy.workflows.versions.diff.compare";
const OLD_LABEL = "workbuddy.workflows.versions.diff.old";
const EMPTY = "workbuddy.workflows.versions.diff.empty";
const MISSING = "workbuddy.workflows.versions.diff.missingChanges";
const OLD_VALUE = "gpt-4o";

function version(id: string, number: number): WorkflowVersion {
  return {
    id,
    workflow_id: WORKFLOW_ID,
    version_number: number,
    origin: "save",
    change_summary: `Change set ${number}`,
    base_version_id: null,
    source_version_id: null,
    created_by: 7,
    created_at: 1_760_000_000 + number,
    is_active: number === 2,
    is_shadow: false,
    is_candidate: false,
  };
}

// Newest first, the order the list route returns.
const VERSIONS = [version(V2_ID, 2), version(V1_ID, 1)];

const SHA_V1 = "a".repeat(64);
const SHA_V2 = "b".repeat(64);

/** The payload the diff route answers with; each case replaces it. */
let diffPayload: unknown;

beforeEach(() => {
  diffPayload = {
    workflow_id: WORKFLOW_ID,
    from: {
      version_id: V1_ID,
      version_number: 1,
      definition_sha256: SHA_V1,
      origin: "save",
    },
    to: {
      version_id: V2_ID,
      version_number: 2,
      definition_sha256: SHA_V2,
      origin: "save",
    },
    changes: [
      {
        path: "/nodes/grade/params/model",
        kind: "replaced",
        old: OLD_VALUE,
        new: "gpt-4o-mini",
      },
    ],
    summary: { added: 0, removed: 0, replaced: 1 },
  };
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (path === VERSIONS_PATH) {
      return { data: { items: VERSIONS }, request_id: "test" };
    }
    if (String(path).startsWith(DIFF_PATH)) {
      return { data: diffPayload, request_id: "test" };
    }
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
});

/** Mount the panel, tick both rows and press the compare button. */
async function compareBothVersions() {
  const user = userEvent.setup();
  render(
    <VersionsPanel
      workflowId={WORKFLOW_ID}
      onSelectWorkflow={() => {}}
      options={[{ value: WORKFLOW_ID, label: "Grade papers" }]}
      optionsLoading={false}
      onWorkflowChanged={() => {}}
    />,
  );
  await screen.findByText("v1");
  // One checkbox per row and no select-all: a comparison is exactly two versions.
  const checkboxes = screen.getAllByRole("checkbox");
  expect(checkboxes).toHaveLength(VERSIONS.length);
  await user.click(checkboxes[0]);
  await user.click(checkboxes[1]);
  await user.click(screen.getByRole("button", { name: COMPARE }));
  return user;
}

describe("VersionsPanel definition diff", () => {
  it("requests the diff of the two selected versions, older version first", async () => {
    await compareBothVersions();

    await waitFor(() => {
      const call = vi
        .mocked(request)
        .mock.calls.find(([path]) => String(path).startsWith(DIFF_PATH));
      // Rows are listed newest first, so the tick order is v2 then v1: the
      // request still has to read from v1 to v2.
      expect(call?.[0]).toBe(`${DIFF_PATH}?from=${V1_ID}&to=${V2_ID}`);
    });
  });

  it("shows each change with its path and kind, sides collapsed", async () => {
    const user = await compareBothVersions();

    expect(await screen.findByText("/nodes/grade/params/model")).toBeTruthy();
    // Grouped under the pointer's container, so the block is readable as one place.
    expect(screen.getByText("/nodes/grade/params")).toBeTruthy();
    expect(screen.getByText("replaced")).toBeTruthy();
    // Both sides are collapsed: neither value is in the document yet.
    expect(screen.queryByText(OLD_VALUE)).toBeNull();

    await user.click(screen.getByText(OLD_LABEL));
    expect(await screen.findByText(OLD_VALUE)).toBeTruthy();
  });

  it("reports an empty change list as no differences", async () => {
    diffPayload = {
      workflow_id: WORKFLOW_ID,
      from: null,
      to: null,
      changes: [],
      summary: { added: 0, removed: 0, replaced: 0 },
    };
    await compareBothVersions();

    expect(await screen.findByText(EMPTY)).toBeTruthy();
    expect(screen.queryByText(MISSING)).toBeNull();
  });

  it("does not read a response without a change list as 'no differences'", async () => {
    diffPayload = { workflow_id: WORKFLOW_ID, from: null, to: null };
    await compareBothVersions();

    expect(await screen.findByText(MISSING)).toBeTruthy();
    expect(screen.queryByText(EMPTY)).toBeNull();
  });

  it("shows the server's reason when the pair cannot be compared", async () => {
    vi.mocked(request).mockImplementation((async (path: string) => {
      if (path === VERSIONS_PATH) {
        return { data: { items: VERSIONS }, request_id: "test" };
      }
      throw new Error(
        'Request failed: 400 Bad Request - {"code":"WORKBUDDY_VERSION_NOT_FOUND",' +
          '"message":"version 11111111-1111-4111-8111-111111111111 belongs to another workflow"}',
      );
    }) as typeof request);
    await compareBothVersions();

    expect(
      await screen.findByText("version 11111111-1111-4111-8111-111111111111 belongs to another workflow"),
    ).toBeTruthy();
  });
});
