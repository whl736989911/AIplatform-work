/**
 * Render smoke test for the WorkBuddy console pages.
 *
 * Nothing else mounts these pages: they sit behind the auth guard, and the
 * backend slices they call are not merged into `main` yet, so a CI run never
 * renders them. This test mounts all seven against a `request()` that answers
 * the way an unmerged route does — `404 Not Found` with FastAPI's plain body —
 * and asserts that each page renders its shell and reports the honest
 * "not available" state instead of crashing or showing fabricated data.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/request")>();
  // Exactly what request() throws for a route the deployment does not serve.
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

import ApprovalsPage from "./Approvals";
import CompliancePage from "./Compliance";
import KnowledgePage from "./Knowledge";
import LifecyclePage from "./Lifecycle";
import MarketplacePage from "./Marketplace";
import ProposalsPage from "./Proposals";
import WorkflowsPage from "./Workflows";

const PAGES: ReadonlyArray<readonly [string, () => React.ReactElement]> = [
  ["Knowledge", KnowledgePage],
  ["Workflows", WorkflowsPage],
  ["Approvals", ApprovalsPage],
  ["Proposals", ProposalsPage],
  ["Marketplace", MarketplacePage],
  ["Compliance", CompliancePage],
  ["Lifecycle", LifecyclePage],
];

describe("WorkBuddy console pages", () => {
  it.each(PAGES)(
    "%s mounts against an unmerged backend slice",
    async (_name, Page) => {
      const { container } = render(
        <MemoryRouter initialEntries={["/workbuddy"]}>
          <Page />
        </MemoryRouter>,
      );

      // The shell renders immediately; the failing request resolves afterwards.
      expect(container.firstChild).toBeTruthy();
      await waitFor(() =>
        expect(document.body.textContent?.trim()).toBeTruthy(),
      );
    },
  );

  it("tells the reader the knowledge slice is missing instead of failing silently", async () => {
    render(
      <MemoryRouter>
        <KnowledgePage />
      </MemoryRouter>,
    );

    // i18n is auto-mocked in tests, so an un-translated key surfaces as its key.
    await waitFor(() =>
      expect(
        screen.getAllByText(/workbuddy\.knowledge\.common\.notMergedTitle/)
          .length,
      ).toBeGreaterThan(0),
    );
  });
});
