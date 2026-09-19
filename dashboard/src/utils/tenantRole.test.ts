/**
 * The governance gate must accept every role the backend accepts.
 *
 * The bug this file exists for: the enterprise panel asked
 * `membership.role === "admin"`, but a tenant is provisioned with an *owner*, so
 * an owner saw the read-only member view while the API would have let them
 * manage members, invitations, quotas and credentials.
 */

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { TENANT_ADMIN_ROLES, isTenantAdminRole } from "./tenantRole";

describe("isTenantAdminRole", () => {
  it.each(["owner", "admin"])("accepts %s", (role) => {
    expect(isTenantAdminRole(role)).toBe(true);
  });

  it.each(["member", "", "OWNER", "Admin"])("refuses %s", (role) => {
    expect(isTenantAdminRole(role)).toBe(false);
  });

  it("refuses a missing membership", () => {
    expect(isTenantAdminRole(undefined)).toBe(false);
    expect(isTenantAdminRole(null)).toBe(false);
  });
});

describe("tenant role parity with the backend", () => {
  it("lists exactly the roles the API treats as tenant administrators", () => {
    // The suite runs from `dashboard/`, but tolerate being run from the root.
    const candidates = [
      "../src/octop/infra/workbuddy/roles.py",
      "src/octop/infra/workbuddy/roles.py",
    ].map((relative) => resolve(process.cwd(), relative));
    const path = candidates.find((candidate) => existsSync(candidate));
    expect(path, `roles.py not found in ${candidates.join(", ")}`).toBeDefined();
    const backend = readFileSync(path as string, "utf8");
    // `frozenset({"owner", "admin"})` — accept either literal form.
    const declared = /TENANT_ADMIN_ROLES\s*=\s*frozenset\([[{]([^\]}]*)[\]}]/.exec(
      backend,
    );
    expect(declared, "TENANT_ADMIN_ROLES not found in roles.py").not.toBeNull();

    const backendRoles = (declared?.[1] ?? "")
      .split(",")
      .map((role) => role.trim().replace(/^["']|["']$/g, ""))
      .filter(Boolean)
      .sort();

    expect([...TENANT_ADMIN_ROLES].sort()).toEqual(backendRoles);
  });
});
