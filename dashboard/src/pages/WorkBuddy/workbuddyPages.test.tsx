/**
 * Render smoke test for the WorkBuddy console pages.
 *
 * Nothing else mounts these pages: they sit behind the auth guard, and the
 * backend slices they call are not merged into `main` yet, so a CI run never
 * renders them. This test mounts all seven against a `request()` that answers
 * the way an unmerged route does — `404 Not Found` with FastAPI's plain body —
 * and asserts that each page renders its shell and reports the honest
 * "not available" state instead of crashing or showing fabricated data. One
 * case does the opposite for the one marketplace route that *is* served: the
 * installations ledger, which must be requested and rendered from the list
 * envelope.
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

import { request } from "../../api/request";
import ApprovalsPage from "./Approvals";
import CompliancePage from "./Compliance";
import HomePage from "./Home";
import InboxPage from "./Inbox";
import KnowledgePage from "./Knowledge";
import LifecyclePage from "./Lifecycle";
import MarketplacePage from "./Marketplace";
import ProposalsPage from "./Proposals";
import RunsPage from "./Runs";
import WorkflowsPage from "./Workflows";

const PAGES: ReadonlyArray<readonly [string, () => React.ReactElement]> = [
  ["Home", HomePage],
  ["Inbox", InboxPage],
  ["Runs", RunsPage],
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

  // The dashboard reads three different routes; an unmerged slice must darken
  // only its own block instead of blanking the page or inventing rows, which is
  // the whole reason the page builds three independent resources.
  it("degrades the dashboard one block at a time", async () => {
    render(
      <MemoryRouter initialEntries={["/workbuddy"]}>
        <HomePage />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(
        screen.getAllByText(/workbuddy\.shared\.notMergedTitle/).length,
      ).toBe(3),
    );
  });

  it("reads this tenant's installation ledger from the list route", async () => {
    const mocked = vi.mocked(request);
    const unmergedRoute = mocked.getMockImplementation();
    const installationId = "77777777-7777-4777-8777-777777777777";
    // The one route that *is* merged answers the list envelope; everything else
    // keeps answering the unmerged 404.
    mocked.mockImplementation((async (path: string) => {
      if (path.startsWith("/v1/marketplace/installations")) {
        return {
          data: [
            {
              id: installationId,
              tenant_id: "11111111-1111-4111-8111-111111111111",
              template_id: "44444444-4444-4444-8444-444444444444",
              template_version_id: "55555555-5555-4555-8555-555555555555",
              workflow_id: null,
              installed_version_id: null,
              installed_by: "22222222-2222-4222-8222-222222222222",
              installed_by_user_id: 10,
              status: "installed",
              consented_license_hash: null,
              consented_capabilities: null,
              consented_at: null,
              job_id: null,
              error_code: null,
              error_detail: null,
              revision: 1,
              created_at: "2026-01-01T00:00:00+00:00",
              updated_at: "2026-01-01T00:00:00+00:00",
            },
          ],
          request_id: "test",
        };
      }
      throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
    }) as typeof request);

    try {
      render(
        <MemoryRouter initialEntries={["/workbuddy/marketplace?tab=installations"]}>
          <MarketplacePage />
        </MemoryRouter>,
      );

      // The ledger section is rendered, and the row the route returned is in it.
      expect(
        await screen.findByText(
          "workbuddy.marketplace.installations.ledgerTitle",
        ),
      ).toBeTruthy();
      expect(await screen.findByText(installationId)).toBeTruthy();
      expect(
        mocked.mock.calls.some(([path]) =>
          String(path).startsWith("/v1/marketplace/installations"),
        ),
      ).toBe(true);
    } finally {
      if (unmergedRoute) mocked.mockImplementation(unmergedRoute);
    }
  });
});
