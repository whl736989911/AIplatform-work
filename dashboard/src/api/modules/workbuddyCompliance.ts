/**
 * WorkBuddy compliance-checklist API — all paths are relative to ``/api/v1``
 * (``request()`` adds the ``/api`` prefix).
 *
 * Frozen manifest (contracts/route-manifest.json, stage C):
 *   GET  /v1/compliance-checklists
 *   POST /v1/compliance-checklists
 *   POST /v1/compliance-checklists/{id}/versions
 *   GET  /v1/compliance-checklists/{id}/versions/{version_id}
 *   POST /v1/compliance-checklists/{id}/versions/{version_id}/approve
 *   POST /v1/compliance-checklists/{id}/versions/{version_id}/revoke
 *   POST /v1/compliance-checklists/{id}/archive
 *
 * Source of the field names: the route manifest is the only frozen source for
 * this slice. No branch implements a compliance-checklist router or service
 * (``feat/workbuddy-c-marketplace`` carries the marketplace service, not a
 * checklist one), so the payload shapes below are derived from the manifest
 * rows themselves — visibility (members read approved, managers also see
 * drafts), immutable revisions whose rules are explicitly "not executable",
 * admin-only approve/revoke that never rewrite history, and archive. Anything
 * the manifest does not pin down (the revision's rule document, the exported
 * status vocabulary) is typed as a tolerant field and rendered defensively,
 * never assumed. Replace these types with the router's own projection the day
 * it lands.
 *
 * The manifest exposes no "list versions" and no "read checklist" route, so a
 * checklist payload is the only place the revision timeline can come from;
 * ``versions`` is therefore tolerated as absent and the UI shows an empty
 * timeline instead of inventing entries.
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

/** ``data`` is a bare array here, but accept ``{ items }`` for consistency. */
async function unwrapList<T>(path: string, init?: RequestInit): Promise<T[]> {
  const body = await unwrap<T[] | { items: T[] }>(path, init);
  return Array.isArray(body) ? body : body.items;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return body === undefined
    ? { method }
    : { method, body: JSON.stringify(body) };
}

/**
 * True when the server has no such surface yet: an unmounted slice answers 404
 * and an unimplemented feature 501. A transport failure is not "unavailable":
 * it falls through to the normal error path instead of claiming the feature
 * does not exist. Callers render an explicit "not available yet" panel for
 * these instead of an error toast, and never substitute fake data.
 */
export function isComplianceUnavailableError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error ?? "");
  return (
    /\b404\b/.test(text) ||
    /\b501\b/.test(text) ||
    /not implemented/i.test(text)
  );
}

// --- Checklists -------------------------------------------------------------

/** Checklist status vocabulary implied by the manifest's draft/approved/archived rows. */
export type ComplianceChecklistStatus = "draft" | "approved" | "archived";

/** Revision status vocabulary: immutable revisions move draft → approved → revoked. */
export type ComplianceVersionStatus = "draft" | "approved" | "revoked";

/**
 * One declarative rule of an immutable revision. The manifest states rules are
 * never executed, so a rule is evidence text, not a machine-checkable
 * predicate.
 */
export interface ComplianceRule {
  /** Rule statement shown to reviewers. */
  statement: string;
  /** Optional supporting detail for the statement. */
  detail?: string | null;
}

/** One immutable checklist revision. */
export interface ComplianceChecklistVersion {
  id: string;
  status: ComplianceVersionStatus;
  rules?: ComplianceRule[];
  created_at: number | null;
  approved_at: number | null;
  revoked_at: number | null;
}

/** One compliance checklist plus the revisions visible to the caller. */
export interface ComplianceChecklist {
  id: string;
  name: string;
  description: string | null;
  status: ComplianceChecklistStatus;
  created_at: number | null;
  updated_at: number | null;
  archived_at: number | null;
  /** Tolerated as absent — see the module header. */
  versions?: ComplianceChecklistVersion[] | null;
}

export interface ComplianceChecklistCreate {
  name: string;
  description?: string | null;
}

/** A revision of the rules; the server freezes it on creation. */
export interface ComplianceChecklistVersionCreate {
  rules: ComplianceRule[];
}

export const workbuddyComplianceApi = {
  /** Members see approved checklists; checklist managers additionally see drafts. */
  listChecklists: () =>
    unwrapList<ComplianceChecklist>(`${BASE}/compliance-checklists`),

  createChecklist: (body: ComplianceChecklistCreate) =>
    unwrap<ComplianceChecklist>(
      `${BASE}/compliance-checklists`,
      jsonInit("POST", body),
    ),

  /** Freeze a new immutable revision of the rules. */
  createChecklistVersion: (
    checklistId: string,
    body: ComplianceChecklistVersionCreate,
  ) =>
    unwrap<ComplianceChecklistVersion>(
      `${BASE}/compliance-checklists/${encodeURIComponent(
        checklistId,
      )}/versions`,
      jsonInit("POST", body),
    ),

  /** Read one pinned revision. */
  getChecklistVersion: (checklistId: string, versionId: string) =>
    unwrap<ComplianceChecklistVersion>(
      `${BASE}/compliance-checklists/${encodeURIComponent(
        checklistId,
      )}/versions/${encodeURIComponent(versionId)}`,
    ),

  /** Admin-only; approving never rewrites a historical report. */
  approveChecklistVersion: (checklistId: string, versionId: string) =>
    unwrap<ComplianceChecklistVersion>(
      `${BASE}/compliance-checklists/${encodeURIComponent(
        checklistId,
      )}/versions/${encodeURIComponent(versionId)}/approve`,
      jsonInit("POST", {}),
    ),

  /** Admin-only; revoking keeps the revision and its history visible. */
  revokeChecklistVersion: (checklistId: string, versionId: string) =>
    unwrap<ComplianceChecklistVersion>(
      `${BASE}/compliance-checklists/${encodeURIComponent(
        checklistId,
      )}/versions/${encodeURIComponent(versionId)}/revoke`,
      jsonInit("POST", {}),
    ),

  /** Archive the checklist itself; admin or checklist owner. */
  archiveChecklist: (checklistId: string) =>
    unwrap<ComplianceChecklist>(
      `${BASE}/compliance-checklists/${encodeURIComponent(
        checklistId,
      )}/archive`,
      jsonInit("POST", {}),
    ),
};
