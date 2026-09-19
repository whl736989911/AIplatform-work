/**
 * WorkBuddy enterprise (tenant) governance API — all paths are relative to
 * ``/api/v1`` (``request()`` adds the ``/api`` prefix).
 *
 * Manifest (contracts/route-manifest.json, stage A2):
 *   GET    /v1/tenant-context
 *   GET    /v1/users                              PATCH /v1/users/{id}
 *   GET    /v1/departments                        POST  /v1/departments
 *   PATCH  /v1/departments/{id}
 *   POST   /v1/invitations                        POST  /v1/invitations/{id}/revoke
 *   GET    /v1/tenant-quotas                      PUT   /v1/tenant-quotas
 *   GET    /v1/connector-credentials              POST  /v1/connector-credentials
 *   POST   /v1/connector-credentials/{id}/rotate
 *   POST   /v1/connector-credentials/{id}/revoke
 *   GET    /v1/connector-credentials/{id}/grants
 *   POST   /v1/connector-credentials/{id}/grants
 *   DELETE /v1/connector-credentials/{id}/grants/{user_id}
 *   GET    /v1/tool-catalog                       GET   /v1/model-catalog
 *   GET    /v1/tenant-capabilities                PUT   /v1/tenant-capabilities
 *
 * Two invariants this module never breaks:
 *  - No raw credential secret is ever returned by the API, so nothing here
 *    reads or stores one beyond the request body the caller typed.
 *  - The one-time invitation token appears only in the creation response;
 *    list/metadata shapes never carry it.
 */

import { request } from "../request";

const BASE = "/v1";

/** Octop user / member identifiers arrive as either DB ints or UUID strings. */
export type UserRef = string | number;

/** Every success body is wrapped: ``{ data, request_id }``. */
export interface ApiEnvelope<T> {
  data: T;
  request_id?: string;
}

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

/**
 * ``data`` is a bare array in the identity slice (users / departments /
 * invitations) and ``{ items }`` in the catalog slice. Accept both so the
 * dashboard works against either envelope shape.
 */
async function unwrapList<T>(path: string, init?: RequestInit): Promise<T[]> {
  const body = await unwrap<T[] | { items: T[] }>(path, init);
  return Array.isArray(body) ? body : body.items;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

// --- Tenant context --------------------------------------------------------

export interface TenantSummary {
  id: string;
  name: string;
  slug: string;
  status: string;
  plan: string | null;
  data_region: string | null;
}

export type TenantMembershipRole = "admin" | "member";

export interface TenantMembership {
  id: string;
  user_id: UserRef;
  role: TenantMembershipRole;
  status: string;
  department_id: string | null;
}

export interface TenantContext {
  tenant: TenantSummary;
  membership: TenantMembership;
}

// --- Users (members) -------------------------------------------------------

export type TenantUserRole = "owner" | "admin" | "member";
export type TenantUserStatus = "active" | "suspended";

export interface TenantUser {
  id: string;
  email: string;
  display_name: string | null;
  role: TenantUserRole;
  department_id: string | null;
  status: TenantUserStatus | string;
  created_at: number | null;
}

export interface TenantUserUpdate {
  display_name?: string | null;
  role?: TenantUserRole;
  department_id?: string | null;
  status?: TenantUserStatus;
}

// --- Departments -----------------------------------------------------------

export interface Department {
  id: string;
  name: string;
  description?: string | null;
  parent_id: string | null;
  status: string;
  member_count?: number;
  created_at?: number | null;
  updated_at?: number | null;
}

export interface DepartmentCreate {
  name: string;
  parent_id?: string | null;
}

export interface DepartmentUpdate {
  name?: string;
  parent_id?: string | null;
  status?: string;
}

// --- Invitations -----------------------------------------------------------

export interface Invitation {
  id: string;
  email: string;
  role: TenantUserRole | string;
  department_id: string | null;
  status: string;
  expires_at: number | null;
  created_at: number | null;
  invited_by?: UserRef | null;
  revoked_at?: number | null;
  accepted_at?: number | null;
}

export interface InvitationCreate {
  email: string;
  role?: TenantUserRole;
  department_id?: string | null;
  expires_in_hours?: number;
}

/** Creation response: metadata plus the one-time raw token (never re-sent). */
export interface InvitationCreated extends Invitation {
  /** Raw one-time token as emitted by the identity slice. */
  invite_token?: string;
  /** Frozen-contract spelling; the server emits `invite_token`. */
  invitation_token?: string;
}

// --- Quotas ----------------------------------------------------------------

/** One metric row: tenant limit, platform ceiling and current usage. */
export interface QuotaMetric {
  metric: string;
  limit: number;
  hard_cap: number | null;
  used: number | null;
  unit: string | null;
  updated_at: number | null;
}

export interface TenantQuotas {
  items: QuotaMetric[];
  /** metric → tenant limit. */
  quotas: Record<string, number>;
  /** metric → platform hard cap. */
  hard_caps: Record<string, number>;
}

export interface TenantQuotasUpdate {
  quotas: Record<string, number>;
}

// --- Connector credentials -------------------------------------------------

export interface ConnectorCredential {
  id: string;
  connector_type: string;
  display_name: string;
  description: string | null;
  owner_id: UserRef;
  status: "active" | "revoked" | string;
  revision: number;
  allowed_scopes: string[];
  rotated_at: number | null;
  revoked_at: number | null;
  created_at: number | null;
  updated_at: number | null;
}

export interface ConnectorCredentialCreate {
  connector_type: string;
  display_name: string;
  secret: Record<string, unknown> | string;
  allowed_scopes: string[];
}

export interface CredentialGrant {
  id: string;
  credential_id: string;
  user_id: UserRef;
  granted_by: UserRef;
  created_at: number | null;
}

// --- Catalog + capabilities ------------------------------------------------

/** Published tool/model revision metadata — no adapter wiring is exposed. */
export interface CatalogItem {
  id: string;
  tool_key?: string | null;
  model_key?: string | null;
  adapter_key?: string | null;
  display_name?: string | null;
  description?: string | null;
  revision?: number | null;
  status?: string | null;
  published_at?: number | null;
  revoked_at?: number | null;
}

/** Identifier shown for a catalog row, tolerating either key spelling. */
export function catalogItemKey(item: CatalogItem): string {
  return item.tool_key ?? item.model_key ?? item.id;
}

export function catalogItemLabel(item: CatalogItem): string {
  return item.display_name?.trim() || catalogItemKey(item);
}

export interface TenantCapabilities {
  tenant_id: string;
  tool_ids: string[];
  model_ids: string[];
  default_model_id: string | null;
  revision: number;
  updated_at: number | null;
}

export interface TenantCapabilitiesUpdate {
  tool_ids: string[];
  model_ids: string[];
  default_model_id: string | null;
}

export const enterpriseApi = {
  // Tenant identity — the only client source of the caller's tenant role.
  tenantContext: () => unwrap<TenantContext>(`${BASE}/tenant-context`),

  // Users
  listUsers: () => unwrapList<TenantUser>(`${BASE}/users`),
  updateUser: (id: string, body: TenantUserUpdate) =>
    unwrap<TenantUser>(`${BASE}/users/${id}`, jsonInit("PATCH", body)),

  // Departments
  listDepartments: () => unwrapList<Department>(`${BASE}/departments`),
  createDepartment: (body: DepartmentCreate) =>
    unwrap<Department>(`${BASE}/departments`, jsonInit("POST", body)),
  updateDepartment: (id: string, body: DepartmentUpdate) =>
    unwrap<Department>(`${BASE}/departments/${id}`, jsonInit("PATCH", body)),

  // Invitations — the raw token only ever exists in createInvitation().
  listInvitations: () => unwrapList<Invitation>(`${BASE}/invitations`),
  createInvitation: (body: InvitationCreate) =>
    unwrap<InvitationCreated>(`${BASE}/invitations`, jsonInit("POST", body)),
  revokeInvitation: (id: string) =>
    unwrap<Invitation>(
      `${BASE}/invitations/${id}/revoke`,
      jsonInit("POST", {}),
    ),

  // Quotas
  getQuotas: () => unwrap<TenantQuotas>(`${BASE}/tenant-quotas`),
  updateQuotas: (body: TenantQuotasUpdate) =>
    unwrap<TenantQuotas>(`${BASE}/tenant-quotas`, jsonInit("PUT", body)),

  // Connector credential metadata + grants (never the secret itself).
  listCredentials: () =>
    unwrap<{ items: ConnectorCredential[] }>(`${BASE}/connector-credentials`),
  createCredential: (body: ConnectorCredentialCreate) =>
    unwrap<ConnectorCredential>(
      `${BASE}/connector-credentials`,
      jsonInit("POST", body),
    ),
  rotateCredential: (id: string, secret: Record<string, unknown> | string) =>
    unwrap<ConnectorCredential>(
      `${BASE}/connector-credentials/${id}/rotate`,
      jsonInit("POST", { secret }),
    ),
  revokeCredential: (id: string) =>
    unwrap<ConnectorCredential>(
      `${BASE}/connector-credentials/${id}/revoke`,
      jsonInit("POST", {}),
    ),
  listGrants: (id: string) =>
    unwrap<{ items: CredentialGrant[] }>(
      `${BASE}/connector-credentials/${id}/grants`,
    ),
  createGrant: (id: string, userId: UserRef) =>
    unwrap<CredentialGrant>(
      `${BASE}/connector-credentials/${id}/grants`,
      jsonInit("POST", { user_id: userId }),
    ),
  deleteGrant: (id: string, userId: UserRef) =>
    unwrap<{ credential_id: string; user_id: UserRef; revoked: boolean }>(
      `${BASE}/connector-credentials/${id}/grants/${encodeURIComponent(
        String(userId),
      )}`,
      { method: "DELETE" },
    ),

  // Catalogs (any member, read-only) + tenant capabilities (tenant admin).
  toolCatalog: () => unwrap<{ items: CatalogItem[] }>(`${BASE}/tool-catalog`),
  modelCatalog: () => unwrap<{ items: CatalogItem[] }>(`${BASE}/model-catalog`),
  getCapabilities: () =>
    unwrap<TenantCapabilities>(`${BASE}/tenant-capabilities`),
  updateCapabilities: (body: TenantCapabilitiesUpdate) =>
    unwrap<TenantCapabilities>(
      `${BASE}/tenant-capabilities`,
      jsonInit("PUT", body),
    ),
};
