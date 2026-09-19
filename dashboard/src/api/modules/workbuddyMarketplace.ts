/**
 * WorkBuddy template marketplace API — all paths are relative to ``/api/v1``
 * (``request()`` adds the ``/api`` prefix).
 *
 * Frozen manifest (contracts/route-manifest.json, stage C):
 *   GET    /v1/marketplace/templates
 *   GET    /v1/marketplace/templates/{id}/versions/{version_id}
 *   POST   /v1/marketplace/templates/{id}/install
 *   GET    /v1/marketplace/installations/{id}
 *   POST   /v1/marketplace/installations/{id}/upgrade
 *   POST   /v1/marketplace/submissions
 *   GET    /v1/marketplace/submissions/{id}
 *   POST   /v1/marketplace/submissions/{id}/submit
 *   POST   /v1/platform/submissions/{tenant_id}/{submission_id}/decisions
 *
 * The HTTP router for this slice is still a placeholder on
 * ``feat/workbuddy-c-marketplace`` (it mounts no routes yet), so every call
 * here answers 404 today. Field names and semantics are taken verbatim from
 * the slice's service layer (``octop.infra.workbuddy.marketplace``) and its row
 * projections (``octop.infra.db.repos.workbuddy_marketplace``); nothing here
 * invents a payload.
 *
 * Two invariants this module never breaks:
 *  - Consent is a caller-supplied acknowledgement. ``capabilitiesDigest()``
 *    computes the exact ``sha256(canonical_json(required_capabilities))`` the
 *    service re-derives on install, and a browser that cannot hash (no
 *    ``crypto.subtle`` outside a secure context) fails closed instead of
 *    sending a consent the server would have to reject.
 *  - Nothing here fabricates success: a failed install/upgrade job is returned
 *    as ``failed`` with the server's ``error_code``, never as an installed row.
 */

import { request } from "../request";

const BASE = "/v1";

/** JSON object as it travels on the wire (definitions, capability rows). */
export type JsonObject = Record<string, unknown>;

/** Every success body is wrapped: ``{ data, request_id }``. */
export interface ApiEnvelope<T> {
  data: T;
  request_id?: string;
}

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

/** ``data`` is a bare array in this slice, but accept ``{ items }`` too. */
async function unwrapList<T>(path: string, init?: RequestInit): Promise<T[]> {
  const body = await unwrap<T[] | { items: T[] }>(path, init);
  return Array.isArray(body) ? body : body.items;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

/**
 * True when the server has no such surface yet: an unmounted slice answers 404
 * (or a proxy answers 501/503). Callers render an explicit "not available yet"
 * panel for these instead of an error toast, and never substitute fake data.
 */
export function isMarketplaceUnavailableError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error ?? "");
  return (
    /\b404\b/.test(text) ||
    /\b501\b/.test(text) ||
    /\b503\b/.test(text) ||
    /not implemented/i.test(text) ||
    /not found/i.test(text) ||
    /failed to fetch|networkerror|network error|load failed/i.test(text)
  );
}

/** Error body of this slice. ``MarketplaceError.to_dict()`` is top-level. */
export interface MarketplaceServerError {
  code?: string;
  message?: string;
  /** JSON path or request field the rejection names. */
  path?: string;
  details?: Record<string, unknown>;
}

/**
 * Read the slice's own error envelope out of a failed ``request()`` call. The
 * marketplace router is unmerged, so both spellings are accepted: the module's
 * top-level ``{code, message, path}`` and the platform wrapper's
 * ``{error: {...}}``.
 */
export function parseMarketplaceServerError(
  error: unknown,
): MarketplaceServerError | null {
  if (!(error instanceof Error)) return null;
  const jsonStart = error.message.indexOf("{");
  if (jsonStart < 0) return null;
  let body: unknown;
  try {
    body = JSON.parse(error.message.slice(jsonStart)) as unknown;
  } catch {
    return null;
  }
  if (body === null || typeof body !== "object") return null;
  const record = body as Record<string, unknown>;
  const nested = record.error;
  const source =
    nested !== null && typeof nested === "object"
      ? (nested as Record<string, unknown>)
      : record;
  const code = typeof source.code === "string" ? source.code : undefined;
  const message =
    typeof source.message === "string" ? source.message : undefined;
  const path = typeof source.path === "string" ? source.path : undefined;
  const details =
    source.details !== null && typeof source.details === "object"
      ? (source.details as Record<string, unknown>)
      : undefined;
  if (!code && !message && !path) return null;
  return { code, message, path, details };
}

// --- Catalogue -------------------------------------------------------------

/** Kind vocabulary of the slice's ``CAPABILITY_KINDS``. */
export type MarketplaceCapabilityKind =
  | "tool"
  | "model"
  | "credential"
  | "knowledge_base"
  | "approver";

/** Tool capabilities declare their side effect; other kinds carry ``null``. */
export type MarketplaceCapabilityEffect = "read_only" | "external_write";

export const MARKETPLACE_SCHEMA_VERSION = 1;

export type MarketplaceTemplateStatus = "published" | "withdrawn";

/** One public declaration a published version makes about what it needs. */
export interface MarketplaceCapabilityDeclaration {
  kind: MarketplaceCapabilityKind;
  /** Tool/model key, or the rebinding slot name for credential/KB/approver. */
  key: string;
  label?: string;
  effect?: MarketplaceCapabilityEffect | null;
  /** Exact platform revision the template pins, when it pins one. */
  revision_id?: string | null;
  /** Reserved rebinding placeholder standing in for a tenant object. */
  placeholder_id?: string | null;
  required?: boolean;
}

/** Catalogue row: published template metadata plus its current version. */
export interface MarketplaceTemplate {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  industry: string | null;
  publisher_display: string | null;
  status: MarketplaceTemplateStatus | string;
  current_version_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** Joined from the current version by ``list_published_templates``. */
  current_version?: string | null;
  license_id?: string | null;
  content_summary?: string | null;
  required_capabilities?: MarketplaceCapabilityDeclaration[];
  published_at?: string | null;
}

/** One immutable published version — the unit an install pins. */
export interface MarketplaceTemplateVersion {
  template_id: string;
  template_version_id: string;
  version: string;
  definition: JsonObject;
  definition_hash: string;
  schema_version: number;
  license_id: string;
  license_text_hash: string;
  required_capabilities: MarketplaceCapabilityDeclaration[];
  content_summary: string | null;
  published_at: string | null;
}

export interface MarketplaceTemplateListQuery {
  industry?: string;
  limit?: number;
  offset?: number;
}

// --- Jobs, installs, upgrades ----------------------------------------------

export type MarketplaceJobStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed";

export type MarketplaceJobKind = "template_install" | "template_upgrade";

export interface MarketplaceJob {
  id: string;
  kind: MarketplaceJobKind | string;
  status: MarketplaceJobStatus | string;
  progress: number;
  requested_by_user_id: number | null;
  result: JsonObject | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export type MarketplaceInstallationStatus =
  | "pending"
  | "installing"
  | "installed"
  | "failed";

export interface MarketplaceInstallation {
  id: string;
  tenant_id: string;
  template_id: string;
  template_version_id: string;
  workflow_id: string | null;
  installed_version_id: string | null;
  installed_by: string;
  installed_by_user_id: number | null;
  status: MarketplaceInstallationStatus | string;
  consented_license_hash: string | null;
  consented_capabilities: MarketplaceCapabilityDeclaration[] | null;
  consented_at: string | null;
  job_id: string | null;
  /** Recorded failure code of the install itself (never a faked success). */
  error_code: string | null;
  error_detail: string | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface MarketplaceCredentialBinding {
  binding_key: string;
  credential_id: string;
  created_at: string | null;
}

/** Consent evidence recorded for one install or upgrade (``InstallConsent``). */
export interface MarketplaceInstallConsent {
  license_id: string;
  license_text_hash: string;
  capabilities: MarketplaceCapabilityDeclaration[];
  capabilities_hash: string;
  consented_at: string;
}

export type MarketplaceUpgradeStatus =
  | "pending"
  | "installing"
  | "installed"
  | "failed";

export interface MarketplaceUpgrade {
  id: string;
  tenant_id: string;
  installation_id: string;
  from_template_version_id: string;
  to_template_version_id: string;
  workflow_id: string | null;
  workflow_version_id: string | null;
  requested_by: string;
  requested_by_user_id: number | null;
  status: MarketplaceUpgradeStatus | string;
  consented_license_hash: string | null;
  consented_capabilities: MarketplaceCapabilityDeclaration[] | null;
  consented_at: string | null;
  job_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Installation detail: state plus consent evidence, bindings and history. */
export interface MarketplaceInstallationDetail extends MarketplaceInstallation {
  job: MarketplaceJob | null;
  credential_bindings: MarketplaceCredentialBinding[];
  consents: MarketplaceInstallConsent[];
  upgrades: MarketplaceUpgrade[];
}

// --- Consent + rebinding ---------------------------------------------------

/**
 * The acknowledgement the service re-validates field by field
 * (``ConsentAcceptance.from_json``). ``accepted`` must be ``true`` — it is the
 * user's explicit action, never a pre-checked default.
 */
export interface MarketplaceConsentInput {
  accepted: true;
  template_version_id: string;
  license_text_hash: string;
  capabilities_hash: string;
}

/** Slot → same-tenant object id; placeholders are refused by the service. */
export interface MarketplaceBindingInput {
  knowledge_bases?: Record<string, string>;
  approvers?: Record<string, string>;
}

/** Slot → active connector credential id of the installing tenant. */
export type MarketplaceCredentialBindings = Record<string, string>;

export interface MarketplaceInstallRequest {
  template_version_id: string;
  consent: MarketplaceConsentInput;
  bindings?: MarketplaceBindingInput;
  credential_bindings?: MarketplaceCredentialBindings;
  /** Defaults server-side to "<version> import" when omitted. */
  workflow_name?: string;
}

export interface MarketplaceUpgradeRequest {
  template_id: string;
  template_version_id: string;
  consent: MarketplaceConsentInput;
  bindings?: MarketplaceBindingInput;
  credential_bindings?: MarketplaceCredentialBindings;
}

/**
 * ``InstallOutcome``: the installation row, the job that carries the result,
 * and — when the router echoes it — the plan the service validated.
 */
export interface MarketplaceInstallResult {
  /** Manifest documents the 202 body as carrying the job id. */
  job_id?: string;
  installation: MarketplaceInstallation;
  job: MarketplaceJob | null;
  plan?: MarketplaceInstallPlan;
}

/** ``InstallPlan`` once validated: what the install was allowed to do. */
export interface MarketplaceInstallPlan {
  template_id: string;
  template_version_id: string;
  version: string;
  workflow_name: string;
  definition_sha256: string;
  credential_bindings: MarketplaceCredentialBindings;
  knowledge_base_ids: string[];
  approver_ids: string[];
}

/** ``UpgradeOutcome``: installation, upgrade row and the upgrade job. */
export interface MarketplaceUpgradeResult {
  job_id?: string;
  installation: MarketplaceInstallation;
  upgrade: MarketplaceUpgrade;
  job: MarketplaceJob | null;
}

// --- Submissions and platform review ---------------------------------------

export type MarketplaceSubmissionStatus =
  | "draft"
  | "submitted"
  | "approved"
  | "rejected";

export type MarketplaceReviewDecision = "approved" | "rejected";

export interface MarketplaceSubmission {
  id: string;
  tenant_id: string;
  submitted_by: string;
  submitted_by_user_id: number | null;
  name: string;
  summary: string;
  industry: string;
  definition: JsonObject;
  definition_hash: string;
  license_id: string;
  /** Only the hash is ever returned — the license text is not echoed back. */
  license_text_hash: string;
  requested_capabilities: MarketplaceCapabilityDeclaration[];
  status: MarketplaceSubmissionStatus | string;
  frozen_definition: JsonObject | null;
  frozen_definition_hash: string | null;
  review_note: string | null;
  platform_review_ref: string | null;
  published_template_version_id: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  reviewed_by_user_id: number | null;
  /** Optimistic-concurrency revision used by submit and decide. */
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface MarketplaceSubmissionReview {
  id: string;
  submission_id: string;
  decision: MarketplaceReviewDecision | string;
  note: string | null;
  platform_review_ref: string | null;
  reviewer_user_id: number | null;
  published_template_version_id: string | null;
  created_at: string | null;
}

/** Submission detail: the row plus its append-only review trail. */
export interface MarketplaceSubmissionDetail extends MarketplaceSubmission {
  reviews: MarketplaceSubmissionReview[];
}

/**
 * Draft payload. ``license_text`` is hashed server-side and never returned —
 * the API only ever exposes ``license_text_hash``.
 */
export interface MarketplaceSubmissionCreateRequest {
  name: string;
  summary?: string;
  industry?: string;
  definition: JsonObject;
  license_id: string;
  license_text: string;
  capabilities?: MarketplaceCapabilityDeclaration[];
}

/** Freeze a draft. ``expected_revision`` is the row revision last read. */
export interface MarketplaceSubmissionSubmitRequest {
  expected_revision?: number;
}

/** Publication metadata an approval needs (``decide_submission``). */
export interface MarketplacePublicationInput {
  slug?: string;
  version: string;
  publisher_display: string;
  description?: string;
  content_summary?: string;
  /** Set to publish a new version of an existing template. */
  template_id?: string;
}

export interface MarketplaceDecisionRequest {
  decision: MarketplaceReviewDecision;
  /** Static-scan reference of the independent platform review. */
  platform_review_ref: string;
  note?: string;
  expected_revision?: number;
  /** Required when ``decision`` is ``approved``. */
  publication?: MarketplacePublicationInput;
}

export interface MarketplaceDecisionResult {
  submission: MarketplaceSubmission;
  review: MarketplaceSubmissionReview;
  published_version: MarketplaceTemplateVersion | null;
}

// --- Canonical digest ------------------------------------------------------

/** Canonical JSON: sorted keys, no insignificant whitespace (service rule). */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  const entries = Object.entries(value as JsonObject)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
    .join(",")}}`;
}

/** Raised when the browser cannot hash (``crypto.subtle`` needs a secure context). */
export class MarketplaceDigestUnavailableError extends Error {
  constructor() {
    super("this browser cannot compute a SHA-256 digest");
    this.name = "MarketplaceDigestUnavailableError";
  }
}

async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new MarketplaceDigestUnavailableError();
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * The exact ``capabilities_hash`` the service re-derives in ``verify_consent``:
 * ``sha256(canonical_json(required_capabilities))`` over the published rows.
 */
export function capabilitiesDigest(
  capabilities: readonly MarketplaceCapabilityDeclaration[],
): Promise<string> {
  return sha256Hex(canonicalJson(capabilities));
}

// --- Client ----------------------------------------------------------------

export const workbuddyMarketplaceApi = {
  /**
   * Published catalogue. There is no list route for versions, installs or
   * submissions in the frozen manifest — callers read those by id.
   */
  listTemplates: (query: MarketplaceTemplateListQuery = {}) => {
    const params = new URLSearchParams();
    if (query.industry?.trim()) params.set("industry", query.industry.trim());
    if (typeof query.limit === "number")
      params.set("limit", String(query.limit));
    if (typeof query.offset === "number")
      params.set("offset", String(query.offset));
    const suffix = params.toString();
    return unwrapList<MarketplaceTemplate>(
      `${BASE}/marketplace/templates${suffix ? `?${suffix}` : ""}`,
    );
  },

  getTemplateVersion: (templateId: string, versionId: string) =>
    unwrap<MarketplaceTemplateVersion>(
      `${BASE}/marketplace/templates/${encodeURIComponent(
        templateId,
      )}/versions/${encodeURIComponent(versionId)}`,
    ),

  /** 202: the install runs as a job; a failure comes back on the job row. */
  installTemplate: (templateId: string, body: MarketplaceInstallRequest) =>
    unwrap<MarketplaceInstallResult>(
      `${BASE}/marketplace/templates/${encodeURIComponent(templateId)}/install`,
      jsonInit("POST", body),
    ),

  getInstallation: (installationId: string) =>
    unwrap<MarketplaceInstallationDetail>(
      `${BASE}/marketplace/installations/${encodeURIComponent(installationId)}`,
    ),

  /** 202: explicit upgrade with renewed consent for the target version. */
  upgradeInstallation: (
    installationId: string,
    body: MarketplaceUpgradeRequest,
  ) =>
    unwrap<MarketplaceUpgradeResult>(
      `${BASE}/marketplace/installations/${encodeURIComponent(
        installationId,
      )}/upgrade`,
      jsonInit("POST", body),
    ),

  /** 201: draft only — nothing is frozen until ``submitSubmission``. */
  createSubmission: (body: MarketplaceSubmissionCreateRequest) =>
    unwrap<MarketplaceSubmission>(
      `${BASE}/marketplace/submissions`,
      jsonInit("POST", body),
    ),

  getSubmission: (submissionId: string) =>
    unwrap<MarketplaceSubmissionDetail>(
      `${BASE}/marketplace/submissions/${encodeURIComponent(submissionId)}`,
    ),

  /** Freezes the sanitized content and moves the draft to ``submitted``. */
  submitSubmission: (
    submissionId: string,
    body: MarketplaceSubmissionSubmitRequest = {},
  ) =>
    unwrap<MarketplaceSubmission>(
      `${BASE}/marketplace/submissions/${encodeURIComponent(
        submissionId,
      )}/submit`,
      jsonInit("POST", body),
    ),

  /** Independent platform review; an approval publishes one frozen version. */
  decidePlatformSubmission: (
    tenantId: string,
    submissionId: string,
    body: MarketplaceDecisionRequest,
  ) =>
    unwrap<MarketplaceDecisionResult>(
      `${BASE}/platform/submissions/${encodeURIComponent(
        tenantId,
      )}/${encodeURIComponent(submissionId)}/decisions`,
      jsonInit("POST", body),
    ),
};
