/**
 * Who gets to govern a tenant.
 *
 * The regression: the panel asked for `membership.role === "admin"`, and a
 * tenant is provisioned with an *owner*, so the first user of every tenant was
 * shown the read-only member view (`memberNotice`) and none of the governance
 * tabs — while the API would have let them manage members, invitations, quotas
 * and credentials. The role list now comes from `utils/tenantRole`, which the
 * backend's `TENANT_ADMIN_ROLES` keeps in step.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TenantContext } from "../../api/modules/enterprise";

const tenantContext = vi.fn<() => Promise<TenantContext>>();

vi.mock("../../api/modules/enterprise", () => ({
  enterpriseApi: {
    tenantContext: () => tenantContext(),
    // The panels read plain arrays (`unwrapList`), not envelopes.
    listDepartments: async () => [],
    listUsers: async () => [],
    listInvitations: async () => [],
    listCatalogs: async () => [],
    tenantQuotas: async () => [],
    listCredentials: async () => [],
    tenantCapabilities: async () => [],
  },
}));

import EnterprisePage from "./index";

function context(role: string): TenantContext {
  return {
    tenant: {
      tenant_id: "11111111-1111-1111-1111-111111111111",
      slug: "demo",
      name: "Demo tenant",
      plan: "standard",
      status: "active",
      data_region: "cn",
      created_at: 1_700_000_000,
    },
    membership: {
      id: "22222222-2222-2222-2222-222222222222",
      tenant_id: "11111111-1111-1111-1111-111111111111",
      user_id: 1,
      email: "admin@example.com",
      display_name: "Admin",
      role: role as TenantContext["membership"]["role"],
      department_id: null,
      status: "active",
      created_at: 1_700_000_000,
    },
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/enterprise"]}>
      <EnterprisePage />
    </MemoryRouter>,
  );
}

describe("enterprise governance gate", () => {
  beforeEach(() => {
    tenantContext.mockReset();
  });

  it("gives a tenant owner the governance tabs", async () => {
    tenantContext.mockResolvedValue(context("owner"));
    renderPage();

    // i18n is auto-mocked, so tabs are readable by their keys.
    await waitFor(() =>
      expect(screen.getAllByText("tenantGovernance.tab.users").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText("tenantGovernance.tab.quotas").length).toBeGreaterThan(0);
    expect(screen.getAllByText("tenantGovernance.tab.credentials").length).toBeGreaterThan(0);
    expect(screen.queryByText("tenantGovernance.memberNoticeHint")).toBeNull();
  });

  it("keeps a plain member read-only", async () => {
    tenantContext.mockResolvedValue(context("member"));
    renderPage();

    await waitFor(() =>
      expect(screen.getAllByText("tenantGovernance.tab.departments").length).toBeGreaterThan(0),
    );
    expect(screen.queryByText("tenantGovernance.tab.users")).toBeNull();
    expect(screen.queryByText("tenantGovernance.tab.quotas")).toBeNull();
    expect(screen.getAllByText("tenantGovernance.memberNoticeHint").length).toBeGreaterThan(0);
  });
});
