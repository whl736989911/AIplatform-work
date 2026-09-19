/**
 * WorkBuddy tenant lifecycle API — controlled export, one-time redeem, deletion.
 * All paths are relative to ``/api/v1`` (``request()`` adds the ``/api`` prefix).
 *
 * Frozen manifest (contracts/route-manifest.json, stages C/D):
 *   POST /v1/tenants/{id}/export
 *   POST /v1/exports/{id}/redeem
 *   POST /v1/exports/{id}/download-challenge              (stage D)
 *   POST /v1/auth/reauthenticate                          (stage D)
 *   POST /v1/tenants/{id}/deletion-requests
 *   GET  /v1/tenants/{id}/deletion-requests/{request_id}
 *   POST /v1/tenants/{id}/deletion-requests/{request_id}/cancel
 *
 * Field names are taken verbatim from the slice on
 * ``feat/workbuddy-compliance-lifecycle``: the router
 * (``octop.api.routers.workbuddy_lifecycle``), ``ExportIssue.as_payload`` /
 * ``ExportDownload`` / ``DeletionView.as_payload`` in
 * ``octop.infra.workbuddy.lifecycle`` and the job status vocabulary in
 * ``octop.infra.db.repos.workbuddy_lifecycle``. The two stage-D routes have no
 * implementation on any branch yet, so their request/response shapes are
 * derived from the manifest rows only and are marked "provisional" below.
 *
 * Two invariants this module never breaks:
 *  - The redeem token returned by ``requestTenantExport`` is the only raw token
 *    this module ever produces; it is handed back to the caller and never
 *    stored, echoed into a path, or logged here.
 *  - Deletion is fail-closed: ``COMPLIANCE_GATE_CLOSED`` is surfaced as an
 *    error code so the UI can show that the entry point is closed. Nothing in
 *    this module synthesises a queued or optimistic deletion state.
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

function jsonInit(method: string, body?: unknown): RequestInit {
  return body === undefined
    ? { method }
    : { method, body: JSON.stringify(body) };
}

/** ``ErrorCode`` members this surface answers with (see ``octop.infra.errors``). */
export const COMPLIANCE_GATE_CLOSED = "COMPLIANCE_GATE_CLOSED";
export const DELETION_CANCEL_WINDOW_CLOSED = "DELETION_CANCEL_WINDOW_CLOSED";
export const DELETION_REQUEST_CONFLICT = "DELETION_REQUEST_CONFLICT";
export const PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED";

/**
 * True when the server has no such surface yet: an unmounted slice answers 404
 * and an unimplemented feature 501. 503 is deliberately *not* treated as
 * "unavailable" — this slice answers ``COMPLIANCE_GATE_CLOSED`` with 503, and
 * that must stay visible as a gate state, never as an empty panel. A transport
 * failure is not "unavailable" either: it falls through to the normal error
 * path instead of claiming the feature does not exist.
 */
export function isLifecycleUnavailableError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error ?? "");
  return (
    /\b404\b/.test(text) ||
    /\b501\b/.test(text) ||
    /not implemented/i.test(text)
  );
}

// --- Export -----------------------------------------------------------------

/** Job status vocabulary of the lifecycle repo (``queued → ready → …``). */
export type ExportJobStatus =
  | "queued"
  | "ready"
  | "failed"
  | "redeemed"
  | "expired";

/**
 * ``ExportIssue.as_payload()`` — creation result of ``POST /tenants/{id}/export``
 * (202). ``redeem_token`` is the single raw one-time token; the server never
 * returns it again and keeps only its sha256.
 */
export interface TenantExportIssue {
  job_id: string;
  export_job_id: string;
  status: ExportJobStatus;
  redeem_token: string;
  redeem_expires_at: number;
  manifest_sha256: string | null;
  table_total: number;
  row_total: number;
}

/** One exported table entry of the frozen manifest (``ExportTable``). */
export interface ExportManifestTable {
  name: string;
  row_count: number;
  columns: string[];
  excluded_columns: string[];
  content_sha256: string;
}

/** One table kept out of the export entirely (``ExportTableExclusion``). */
export interface ExportManifestExclusion {
  name: string;
  category: string;
  included: false;
}

/** Canonical manifest whose sha256 is recorded with the job. */
export interface ExportManifest {
  schema: string;
  redaction_rules_version: number;
  tenant_id: string;
  export_job_id: string;
  created_at: number;
  tables: ExportManifestTable[];
  excluded_tables: ExportManifestExclusion[];
  totals: { tables: number; rows: number; excluded_tables: number };
  content_sha256: string;
}

/** One table of a redeemed payload; rows are already redacted server-side. */
export interface ExportRedeemedTable {
  name: string;
  row_count: number;
  content_sha256: string;
  rows: Record<string, unknown>[];
}

/** Body of a consumed redeem token (``POST /exports/{id}/redeem``, 200). */
export interface ExportDownload {
  export_job_id: string;
  manifest: ExportManifest;
  manifest_sha256: string;
  tables: ExportRedeemedTable[];
  redeemed_at: number;
}

/**
 * Provisional: ``POST /auth/reauthenticate`` (stage D) is documented in the
 * manifest as "issues a five-minute one-time re-authentication credential"
 * bound to tenant/user/purpose. No router implements it on any branch yet, so
 * only the purpose of each field is frozen — treat a mismatch as a server
 * validation error, never as a client-side truth.
 */
export interface ReauthenticateRequest {
  password: string;
  purpose?: string;
}

/** Provisional — see ``ReauthenticateRequest``. */
export interface ReauthenticateResult {
  credential: string;
  expires_at: number;
}

/**
 * Provisional: ``POST /exports/{id}/download-challenge`` (stage D) issues the
 * export redeem challenge for a freshly re-authenticated admin, ``no-store``,
 * without extending the 72h redeem window.
 */
export interface ExportDownloadChallengeRequest {
  credential: string;
}

/** Provisional — see ``ExportDownloadChallengeRequest``. */
export interface ExportDownloadChallenge {
  challenge: string;
  expires_at: number;
}

// --- Deletion ---------------------------------------------------------------

/** Stage vocabulary of ``deletion_timeline`` in the lifecycle policy. */
export type DeletionStage = "cooling_off" | "cancelled" | "archived" | "purged";

/**
 * ``DeletionView.as_payload()`` — the only authority on a deletion request's
 * state. ``cancellable`` is server-computed: cancel is refused once the
 * cooling-off window has closed, a legal hold is active only when the server
 * says so, and nothing here derives either flag locally.
 */
export interface DeletionRequestView {
  deletion_request_id: string;
  tenant_id: string;
  stage: DeletionStage;
  requested_at: number;
  cooling_off_ends_at: number;
  purge_due_at: number | null;
  purged_at: number | null;
  cancellable: boolean;
  legal_hold_active: boolean;
  version: number;
  policy_sha256: string;
  policy_expires_at: number;
  archive_sha256: string | null;
  tombstone_sha256: string | null;
}

/** CAS guard for the cancel route; ``expected_version`` is the loaded view's. */
export interface DeletionCancelRequest {
  expected_version?: number;
}

export const workbuddyLifecycleApi = {
  /**
   * Build the redacted export and mint its single redeem token (202). The
   * caller must display the token exactly once and never persist it.
   */
  requestTenantExport: (tenantId: string) =>
    unwrap<TenantExportIssue>(
      `${BASE}/tenants/${encodeURIComponent(tenantId)}/export`,
      jsonInit("POST", {}),
    ),

  /** Consume the one-time token; a second attempt is refused by the server. */
  redeemExport: (exportJobId: string, token: string) =>
    unwrap<ExportDownload>(
      `${BASE}/exports/${encodeURIComponent(exportJobId)}/redeem`,
      jsonInit("POST", { token }),
    ),

  /** Provisional stage-D route; see ``ExportDownloadChallengeRequest``. */
  requestDownloadChallenge: (
    exportJobId: string,
    body: ExportDownloadChallengeRequest,
  ) =>
    unwrap<ExportDownloadChallenge>(
      `${BASE}/exports/${encodeURIComponent(exportJobId)}/download-challenge`,
      jsonInit("POST", body),
    ),

  /** Provisional stage-D route; see ``ReauthenticateRequest``. */
  reauthenticate: (body: ReauthenticateRequest) =>
    unwrap<ReauthenticateResult>(
      `${BASE}/auth/reauthenticate`,
      jsonInit("POST", body),
    ),

  /**
   * Open the 30-day cooling-off window (201). Without a signed compliance
   * policy the server answers ``COMPLIANCE_GATE_CLOSED`` and queues nothing.
   */
  createDeletionRequest: (tenantId: string) =>
    unwrap<DeletionRequestView>(
      `${BASE}/tenants/${encodeURIComponent(tenantId)}/deletion-requests`,
      jsonInit("POST", {}),
    ),

  /** Read a deletion request's stage and timestamps. */
  getDeletionRequest: (tenantId: string, deletionRequestId: string) =>
    unwrap<DeletionRequestView>(
      `${BASE}/tenants/${encodeURIComponent(
        tenantId,
      )}/deletion-requests/${encodeURIComponent(deletionRequestId)}`,
    ),

  /** CAS cancel: only a live cooling-off request with a matching version wins. */
  cancelDeletionRequest: (
    tenantId: string,
    deletionRequestId: string,
    body?: DeletionCancelRequest,
  ) =>
    unwrap<DeletionRequestView>(
      `${BASE}/tenants/${encodeURIComponent(
        tenantId,
      )}/deletion-requests/${encodeURIComponent(deletionRequestId)}/cancel`,
      jsonInit("POST", body ?? {}),
    ),
};
