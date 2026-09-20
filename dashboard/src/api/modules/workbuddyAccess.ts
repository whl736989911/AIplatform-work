/**
 * WorkBuddy tenant duty grants (/v1/duties) — the access policy surface.
 * All paths are relative to ``/api/v1`` (``request()`` adds the ``/api`` prefix).
 *
 * Manifest (contracts/route-manifest.json, stage B-07):
 *   GET    /v1/duties                every grant plus the duty vocabulary
 *   POST   /v1/duties/{duty}/grants  grant one duty to a tenant / department / member
 *   DELETE /v1/duties/{duty}/grants  revoke that one grant (404 when it is absent)
 *
 * A duty is an extra path to a job, never a power taken away: a tenant admin
 * already holds all five, and a department grant also reaches that
 * department's sub-departments. The vocabulary is closed server-side
 * (``DUTIES`` in ``octop.infra.rbac.duties``): the list response carries it, so
 * the page never hard-codes a sixth duty and renders whatever the server knows.
 */

import { request } from "../request";

const BASE = "/v1";

/** Every success body is wrapped: ``{ data, request_id }``. */
export interface ApiEnvelope<T> {
  data: T;
  request_id?: string;
}

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

// --- Duty vocabulary -------------------------------------------------------

/**
 * The five duties of the tenant vocabulary, as spelled by the backend.
 * ``author`` 作者 (creates and edits), ``publisher`` 发布者, ``approver`` 审批人,
 * ``kb_admin`` 库管理员, ``ops`` 运维.
 */
export type Duty = "author" | "publisher" | "approver" | "kb_admin" | "ops";

/** Who a grant is for: the whole tenant, one department, or one member. */
export type DutySubjectKind = "tenant" | "department" | "member";

/** One grant row: ``subject_id`` is a department id or a member's user id. */
export interface DutyGrant {
  duty: Duty;
  subject_kind: DutySubjectKind;
  /** Department id, member user id as a string, or ``null`` for a tenant grant. */
  subject_id: string | null;
  /** Epoch seconds. */
  granted_at: number;
}

export interface DutyGrantList {
  /** The duty vocabulary in server order — every duty this tenant can grant. */
  duties: Duty[];
  items: DutyGrant[];
}

/** Initial value for a page that renders before the first response arrives. */
export const EMPTY_DUTY_GRANTS: DutyGrantList = { duties: [], items: [] };

/** The subject of a grant; ``subject_id`` is required unless it is the tenant. */
export interface DutySubject {
  subject_kind: DutySubjectKind;
  subject_id?: string | null;
}

/** Result of a revoke: the grant that is now gone, echoed by the server. */
export interface DutyGrantRevoked {
  duty: Duty;
  subject_kind: DutySubjectKind;
  subject_id: string | null;
  revoked: boolean;
}

// --- Members (subject of a member grant) -----------------------------------

/**
 * One row of ``GET /v1/users``, as this surface reads it.
 *
 * The membership ``id`` is not what a duty names: a member subject is the
 * Octop ``users.id`` (``octop.infra.rbac.subjects``), which the identity route
 * exposes as ``user_id``. Only the fields the picker and the row labels need
 * are typed here, so the subject mapping stays inside this module.
 */
export interface AccessMember {
  /** Membership id of the tenant. */
  id: string;
  /** Octop ``users.id`` — the value a member grant is addressed by. */
  user_id?: number | string | null;
  username?: string | null;
  display_name?: string | null;
  email?: string | null;
}

/** Stable identity of one grant, for table keys and row replacement. */
export function dutyGrantKey(grant: {
  duty: Duty;
  subject_kind: DutySubjectKind;
  subject_id: string | null;
}): string {
  return `${grant.duty}:${grant.subject_kind}:${grant.subject_id ?? ""}`;
}

function subjectQuery(subject: DutySubject): string {
  const params = new URLSearchParams({ subject_kind: subject.subject_kind });
  if (subject.subject_id) params.set("subject_id", subject.subject_id);
  return params.toString();
}

export const workbuddyAccessApi = {
  /** Grants of this tenant plus the duty vocabulary (tenant admin only). */
  listDuties: () => unwrap<DutyGrantList>(`${BASE}/duties`),

  /**
   * Tenant members, for the subject picker and the row labels. A member subject
   * is addressed by ``user_id``, never by the membership ``id``; the identity
   * slice answers with a bare array.
   */
  listMembers: () => unwrap<AccessMember[]>(`${BASE}/users`),

  /**
   * Grant one duty. Granting it again to the same subject only refreshes the
   * stamp; every other path to that job stays in place.
   */
  grantDuty: (duty: Duty, subject: DutySubject) =>
    unwrap<DutyGrant>(
      `${BASE}/duties/${encodeURIComponent(duty)}/grants`,
      jsonInit("POST", subject),
    ),

  /** Revoke exactly one grant; the subject keeps every other path to the job. */
  revokeDuty: (duty: Duty, subject: DutySubject) =>
    unwrap<DutyGrantRevoked>(
      `${BASE}/duties/${encodeURIComponent(duty)}/grants?${subjectQuery(
        subject,
      )}`,
      { method: "DELETE" },
    ),
};
