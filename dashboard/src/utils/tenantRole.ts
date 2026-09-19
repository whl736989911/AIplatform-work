/**
 * Tenant-scoped roles that may govern their own enterprise tenant.
 *
 * This mirrors the backend vocabulary in `src/octop/infra/workbuddy/roles.py`
 * (`TENANT_ADMIN_ROLES`) on purpose: a tenant is created with an *owner*, so a
 * UI that only treats `"admin"` as governing would lock every tenant's first
 * user out of the governance panel even though the API would allow them in.
 * `tenantRole.test.ts` asserts the two lists stay equal.
 */
export const TENANT_ADMIN_ROLES = ["owner", "admin"] as const;

export type TenantAdminRole = (typeof TENANT_ADMIN_ROLES)[number];

/** Whether this tenant membership may manage members, invitations, quotas and credentials. */
export function isTenantAdminRole(role: string | null | undefined): boolean {
  return (
    typeof role === "string" &&
    (TENANT_ADMIN_ROLES as readonly string[]).includes(role)
  );
}
