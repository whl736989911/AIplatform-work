/**
 * WorkBuddy workflow definitions, immutable versions, publication, trigger
 * registrations and asynchronous jobs — paths are relative to ``/api/v1``
 * (``request()`` adds the ``/api`` prefix).
 *
 * Manifest (contracts/route-manifest.json, stage A3):
 *   POST   /v1/workflows                                GET /v1/workflows
 *   GET    /v1/workflows/{id}                           PUT /v1/workflows/{id}
 *   POST   /v1/workflows/{id}/activate                  POST /v1/workflows/{id}/rollback
 *   GET    /v1/workflows/{id}/versions                  GET /v1/workflows/{id}/versions/{version_id}
 *   POST   /v1/workflow-definitions/validate
 *   POST   /v1/workflows/{id}/trigger-registrations     GET /v1/workflows/{id}/trigger-registrations
 *   DELETE /v1/workflows/{id}/trigger-registrations/{registration_id}
 *   POST   /v1/workflows/{id}/trigger-registrations/{registration_id}/rotate-secret
 *   POST   /v1/triggers/{registration_id}/test-delivery
 *   GET    /v1/jobs                                     GET /v1/jobs/{id}
 *
 * Two invariants this module never breaks:
 *  - ``PUT`` / ``activate`` / ``rollback`` are compare-and-swap. The server
 *    requires ``If-Match`` carrying its strong ETag over the mutable workflow
 *    row — ``"{workflow_id}.{revision}"`` — and answers 428 when the header is
 *    missing and 409 when it is stale, writing nothing in either case.
 *    ``workflowEtag()`` reproduces that value from a record the caller just
 *    read, and every write response carries the next revision.
 *  - A webhook signing secret exists only in the rotate-secret response. This
 *    module hands it back to the caller and never stores, caches or logs it.
 *
 * Unmounted routers answer 404 and unimplemented ones 501, so every caller
 * must render an explicit "not available yet" state instead of an empty list.
 */

import { request } from "../request";

const BASE = "/v1";

export type JsonObject = Record<string, unknown>;

/** Every WorkBuddy success body is wrapped: ``{ data, request_id }``. */
export interface ApiEnvelope<T> {
  data: T;
  request_id?: string;
}

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

/** All list routes answer ``{ items }``. */
async function unwrapItems<T>(path: string, init?: RequestInit): Promise<T[]> {
  const body = await unwrap<{ items: T[] }>(path, init);
  return body.items;
}

function jsonInit(
  method: string,
  body?: unknown,
  headers?: HeadersInit,
): RequestInit {
  return body === undefined
    ? { method, headers }
    : { method, headers, body: JSON.stringify(body) };
}

function withQuery(
  path: string,
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/**
 * True when this module is not reachable from the running deployment: an
 * unmounted router answers 404, an unimplemented one 501, a deployment
 * without the control-plane database 503, and a stopped server drops the
 * request entirely. Callers render an explicit "not available yet" state for
 * these — never fabricated workflows or a silent empty table.
 */
export function isWorkBuddyUnavailableError(error: unknown): boolean {
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

// --- vocabularies (mirror the PostgreSQL CHECK constraints) ----------------

export const WORKFLOW_STATUSES = ["draft", "active", "archived"] as const;
export type WorkflowStatus = (typeof WORKFLOW_STATUSES)[number];

export const WORKFLOW_VERSION_ORIGINS = [
  "save",
  "rollback",
  "proposal",
  "promotion",
  "import",
] as const;
export type WorkflowVersionOrigin = (typeof WORKFLOW_VERSION_ORIGINS)[number];

export const WORKFLOW_ACTIVATION_MODES = ["active", "shadow"] as const;
export type WorkflowActivationMode = (typeof WORKFLOW_ACTIVATION_MODES)[number];

export const TRIGGER_KINDS = ["cron", "webhook", "event"] as const;
export type TriggerKind = (typeof TRIGGER_KINDS)[number];

export const JOB_KINDS = [
  "execution",
  "improvement_proposal",
  "knowledge_index",
  "export",
  "import",
  "template_install",
  "template_upgrade",
  "trigger_delivery",
] as const;
export type JobKind = (typeof JOB_KINDS)[number];

export const JOB_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

// --- workflows -------------------------------------------------------------

/** Minimal published projection: what an ordinary member may see. */
export interface WorkflowSummary {
  id: string;
  name: string;
  description: string | null;
  status: WorkflowStatus;
  /** Epoch seconds. */
  updated_at: number | null;
}

/** Manager projection (creator or tenant admin): CAS revision + version pointers. */
export interface WorkflowRecord extends WorkflowSummary {
  revision: number;
  active_version_id: string | null;
  shadow_version_id: string | null;
  created_by: number | null;
  created_by_membership_id: string | null;
  /** Epoch seconds. */
  created_at: number | null;
  archived_at: number | null;
}

export type WorkflowListItem = WorkflowSummary | WorkflowRecord;

/** Narrow a list/detail row to the manager projection. */
export function isWorkflowRecord(row: WorkflowListItem): row is WorkflowRecord {
  return "revision" in row && typeof row.revision === "number";
}

/** ``GET /workflows/{id}`` for a member: identity + published input spec only. */
export interface WorkflowMemberDetail extends WorkflowSummary {
  version_id: string | null;
  version_number: number | null;
  inputs: JsonObject;
}

/** ``GET /workflows/{id}`` for a manager: adds the active version definition. */
export interface WorkflowManagerDetail extends WorkflowRecord {
  active_version: WorkflowVersion | null;
}

export type WorkflowDetail = WorkflowMemberDetail | WorkflowManagerDetail;

export function isWorkflowManagerDetail(
  detail: WorkflowDetail,
): detail is WorkflowManagerDetail {
  return "revision" in detail && typeof detail.revision === "number";
}

export interface WorkflowVersion {
  id: string;
  workflow_id: string;
  version_number: number;
  origin: WorkflowVersionOrigin;
  change_summary: string | null;
  base_version_id: string | null;
  source_version_id: string | null;
  created_by: number | null;
  /** Epoch seconds. */
  created_at: number | null;
  is_active: boolean;
  is_shadow: boolean;
  is_candidate: boolean;
  /** Present on version detail and on every write response, absent in lists. */
  definition?: JsonObject;
}

export interface WorkflowCreateRequest {
  name: string;
  description?: string | null;
  definition: JsonObject;
}

export interface WorkflowSaveRequest {
  definition: JsonObject;
  /** Defaults to the active version when omitted. */
  base_version_id?: string | null;
  change_summary?: string | null;
}

export interface WorkflowActivateRequest {
  version_id: string;
  mode?: WorkflowActivationMode;
}

export interface WorkflowRollbackRequest {
  version_id: string;
  change_summary?: string | null;
}

/** Create / save / rollback: the workflow row plus the version that was written. */
export interface WorkflowWithVersion extends WorkflowRecord {
  version: WorkflowVersion;
}

/**
 * Strong validator over the compare-and-swap unit, byte-identical to the
 * server's ``ETag`` (``"{workflow_id}.{revision}"``). Required as ``If-Match``
 * on PUT / activate / rollback.
 */
export function workflowEtag(workflow: {
  id: string;
  revision: number;
}): string {
  return `"${workflow.id}.${workflow.revision}"`;
}

// --- definition validation -------------------------------------------------

export interface DefinitionValidation {
  valid: true;
  definition: JsonObject;
  entry_node_id: string;
  node_count: number;
  edge_count: number;
  exit_node_ids: string[];
  save_as: Record<string, string>;
  semantic_checks: string;
  compiler_version: string;
}

// --- trigger registrations -------------------------------------------------

export type TriggerKnowledgePermission = "read" | "write";

export interface TriggerKnowledgeGrant {
  kb_id: string;
  permission: TriggerKnowledgePermission;
}

export interface TriggerRegistrationGrants {
  tools: string[];
  knowledge_bases: TriggerKnowledgeGrant[];
}

/** Never contains a signing secret: only whether one exists and its version. */
export interface TriggerRegistration {
  registration_id: string;
  workflow_id: string;
  kind: TriggerKind;
  name: string;
  enabled: boolean;
  /** Epoch seconds. */
  revoked_at: number | null;
  webhook_path: string | null;
  cron_expression: string | null;
  event_name: string | null;
  event_filter: JsonObject;
  has_secret: boolean;
  secret_provider: string | null;
  secret_version: number;
  previous_secret_active_until: number | null;
  signature_algorithm: string;
  signature_header: string;
  timestamp_header: string;
  tolerance_seconds: number;
  grants: TriggerRegistrationGrants;
  /** Epoch seconds. */
  created_at: number | null;
  updated_at: number | null;
}

export interface TriggerRegistrationCreate {
  kind: TriggerKind;
  name: string;
  cron_expression?: string | null;
  event_name?: string | null;
  event_filter?: JsonObject | null;
  tool_grants?: string[];
  kb_grants?: TriggerKnowledgeGrant[];
  tolerance_seconds?: number;
  signature_header?: string;
  timestamp_header?: string;
}

export interface TriggerRegistrationRevoked {
  registration_id: string;
  revoked: boolean;
}

/** Rotate response: the secret is present here and in no other response. */
export interface RotatedTriggerSecret {
  registration_id: string;
  secret: string;
  secret_version: number;
  algorithm: string;
  /** Epoch seconds the previous secret stays valid during the overlap. */
  overlap_expires_at: number | null;
}

export interface TriggerTestDelivery {
  delivery_id: string;
  registration_id: string;
  execution_id: string;
  status: string;
  duplicate: boolean;
  test: boolean;
}

// --- jobs ------------------------------------------------------------------

export interface Job {
  id: string;
  kind: JobKind;
  status: JobStatus;
  /** 0–100. */
  progress: number;
  execution_id: string | null;
  result: unknown;
  error_code: string | null;
  error_message: string | null;
  /** ISO-8601. */
  created_at: string | null;
  finished_at: string | null;
}

// --- queries ---------------------------------------------------------------

export interface TriggerRegistrationList {
  items: TriggerRegistration[];
  count: number;
}

export interface JobListQuery {
  status?: JobStatus | "";
  limit?: number;
}

export const workbuddyWorkflowsApi = {
  // Definitions — drafts are compiled before anything is stored.
  listWorkflows: () => unwrapItems<WorkflowListItem>(`${BASE}/workflows`),
  getWorkflow: (id: string) =>
    unwrap<WorkflowDetail>(`${BASE}/workflows/${encodeURIComponent(id)}`),
  createWorkflow: (body: WorkflowCreateRequest) =>
    unwrap<WorkflowWithVersion>(`${BASE}/workflows`, jsonInit("POST", body)),
  saveWorkflowVersion: (id: string, body: WorkflowSaveRequest, etag: string) =>
    unwrap<WorkflowWithVersion>(
      `${BASE}/workflows/${encodeURIComponent(id)}`,
      jsonInit("PUT", body, { "If-Match": etag }),
    ),

  // Publication — both are compare-and-swap writes.
  activateWorkflowVersion: (
    id: string,
    body: WorkflowActivateRequest,
    etag: string,
  ) =>
    unwrap<WorkflowRecord>(
      `${BASE}/workflows/${encodeURIComponent(id)}/activate`,
      jsonInit("POST", body, { "If-Match": etag }),
    ),
  rollbackWorkflowVersion: (
    id: string,
    body: WorkflowRollbackRequest,
    etag: string,
  ) =>
    unwrap<WorkflowWithVersion>(
      `${BASE}/workflows/${encodeURIComponent(id)}/rollback`,
      jsonInit("POST", body, { "If-Match": etag }),
    ),

  // Version history.
  listWorkflowVersions: (id: string) =>
    unwrapItems<WorkflowVersion>(
      `${BASE}/workflows/${encodeURIComponent(id)}/versions`,
    ),
  getWorkflowVersion: (id: string, versionId: string) =>
    unwrap<WorkflowVersion>(
      `${BASE}/workflows/${encodeURIComponent(
        id,
      )}/versions/${encodeURIComponent(versionId)}`,
    ),

  // Definition validation — canonicalizes a draft without persisting it.
  validateDefinition: (definition: JsonObject) =>
    unwrap<DefinitionValidation>(
      `${BASE}/workflow-definitions/validate`,
      jsonInit("POST", { definition }),
    ),

  // Trigger registrations (tenant admins). No response carries a secret.
  listTriggerRegistrations: (workflowId: string) =>
    unwrap<TriggerRegistrationList>(
      `${BASE}/workflows/${encodeURIComponent(
        workflowId,
      )}/trigger-registrations`,
    ),
  createTriggerRegistration: (
    workflowId: string,
    body: TriggerRegistrationCreate,
  ) =>
    unwrap<TriggerRegistration>(
      `${BASE}/workflows/${encodeURIComponent(
        workflowId,
      )}/trigger-registrations`,
      jsonInit("POST", body),
    ),
  revokeTriggerRegistration: (workflowId: string, registrationId: string) =>
    unwrap<TriggerRegistrationRevoked>(
      `${BASE}/workflows/${encodeURIComponent(
        workflowId,
      )}/trigger-registrations/${encodeURIComponent(registrationId)}`,
      { method: "DELETE" },
    ),
  rotateTriggerSecret: (workflowId: string, registrationId: string) =>
    unwrap<RotatedTriggerSecret>(
      `${BASE}/workflows/${encodeURIComponent(
        workflowId,
      )}/trigger-registrations/${encodeURIComponent(
        registrationId,
      )}/rotate-secret`,
      { method: "POST" },
    ),
  testTriggerDelivery: (registrationId: string) =>
    unwrap<TriggerTestDelivery>(
      `${BASE}/triggers/${encodeURIComponent(registrationId)}/test-delivery`,
      { method: "POST" },
    ),

  // Asynchronous jobs — the only way to rediscover an accepted background task.
  listJobs: (query: JobListQuery = {}) =>
    unwrapItems<Job>(
      withQuery(`${BASE}/jobs`, { status: query.status, limit: query.limit }),
    ),
  getJob: (id: string) => unwrap<Job>(`${BASE}/jobs/${encodeURIComponent(id)}`),
};
