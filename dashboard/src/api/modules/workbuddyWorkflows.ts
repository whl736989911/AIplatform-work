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
 *   GET    /v1/workflows/{id}/versions/diff?from=&to=
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
import { parseApiError } from "../../utils/apiError";

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

// --- version comparison (definition-level diff) ----------------------------

/** The three kinds the keyed semantic diff emits. */
export type WorkflowDefinitionChangeKind = "added" | "removed" | "replaced";

/** One difference between two definitions, at one JSON-pointer path. */
export interface WorkflowDefinitionChange {
  path: string;
  /** One of ``WorkflowDefinitionChangeKind``; wider on purpose so a kind a
   * newer server adds still renders instead of being dropped. */
  kind: WorkflowDefinitionChangeKind | string;
  old: unknown;
  new: unknown;
}

/** One compared side. Every field is nullable: a partial payload must not
 * fabricate a version number, an origin or a digest. */
export interface WorkflowVersionDiffSide {
  version_id: string | null;
  version_number: number | null;
  definition_sha256: string | null;
  origin: string | null;
}

export interface WorkflowVersionDiffSummary {
  added: number;
  removed: number;
  replaced: number;
}

/** ``GET /workflows/{id}/versions/diff?from=&to=``. */
export interface WorkflowVersionDiff {
  workflow_id: string | null;
  from: WorkflowVersionDiffSide | null;
  to: WorkflowVersionDiffSide | null;
  /** ``null`` when the response carried no usable change list. An empty array
   * is a real answer ("the two definitions agree") and is never conflated
   * with a missing one. */
  changes: WorkflowDefinitionChange[] | null;
  summary: WorkflowVersionDiffSummary | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asPositiveCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function diffSide(raw: unknown): WorkflowVersionDiffSide | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const number = record.version_number;
  const sha = record.definition_sha256;
  const origin = record.origin;
  const versionId = record.version_id;
  return {
    version_id: typeof versionId === "string" && versionId ? versionId : null,
    version_number: typeof number === "number" && Number.isFinite(number) ? number : null,
    definition_sha256: typeof sha === "string" && sha ? sha : null,
    origin: typeof origin === "string" && origin ? origin : null,
  };
}

function diffChange(raw: unknown): WorkflowDefinitionChange | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const path = record.path;
  if (typeof path !== "string" || !path.trim()) return null;
  const kind = record.kind;
  return {
    path,
    kind: typeof kind === "string" && kind.trim() ? kind : "unknown",
    old: record.old ?? null,
    new: record.new ?? null,
  };
}

function diffSummary(raw: unknown): WorkflowVersionDiffSummary | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const added = asPositiveCount(record.added);
  const removed = asPositiveCount(record.removed);
  const replaced = asPositiveCount(record.replaced);
  if (added === null || removed === null || replaced === null) return null;
  return { added, removed, replaced };
}

/**
 * Tolerant reader for the diff route. A response that is not an object, or
 * whose ``changes`` is not a list of usable entries, reads as ``changes:
 * null`` — the caller then reports "the response carried no change list"
 * instead of pretending the versions are identical.
 */
export function parseWorkflowVersionDiff(raw: unknown): WorkflowVersionDiff {
  const record = asRecord(raw);
  let changes: WorkflowDefinitionChange[] | null = null;
  if (record !== null && Array.isArray(record.changes)) {
    const parsed = record.changes.map(diffChange);
    changes = parsed.every(
      (change): change is WorkflowDefinitionChange => change !== null,
    )
      ? parsed
      : null;
  }
  const workflowId = record?.workflow_id;
  return {
    workflow_id: typeof workflowId === "string" && workflowId ? workflowId : null,
    from: diffSide(record?.from),
    to: diffSide(record?.to),
    changes,
    summary: diffSummary(record?.summary),
  };
}

export interface WorkflowAuthoringRequest {
  /** The description a person wrote. The model never receives a definition. */
  request: string;
  name?: string | null;
  description?: string | null;
}

/** One step as the author described it: what it is, and what it is for. */
export interface AuthoredStep {
  id: string;
  kind: string;
  purpose: string;
  uses: string[];
}

/** Why a generated draft looks the way it does, and how long it took to compile. */
export interface WorkflowAuthoringExplanation {
  rounds: number;
  steps: AuthoredStep[];
}

/** A generated draft, plus the author's own explanation of the steps in it. */
export interface AuthoredWorkflow extends WorkflowWithVersion {
  authoring: WorkflowAuthoringExplanation;
}

export function parseWorkflowAuthoringExplanation(
  raw: unknown,
): WorkflowAuthoringExplanation {
  const record = (raw ?? {}) as Record<string, unknown>;
  const steps = Array.isArray(record.steps) ? record.steps : [];
  return {
    rounds: typeof record.rounds === "number" ? record.rounds : 0,
    steps: steps.map((entry) => {
      const step = (entry ?? {}) as Record<string, unknown>;
      return {
        id: String(step.id ?? ""),
        kind: String(step.kind ?? ""),
        purpose: String(step.purpose ?? ""),
        uses: Array.isArray(step.uses) ? step.uses.map((used) => String(used)) : [],
      };
    }),
  };
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

// --- definition diagnostics (the compiler aggregates per phase) -------------

/**
 * One reason the compiler refused a definition. `node_id` is derived from
 * `path` server-side, and `hint_key` names the i18n key of its repair hint —
 * never prose, so a code without a translation shows the server message rather
 * than an invented suggestion.
 */
export interface WorkflowDiagnostic {
  code: string;
  message: string;
  path?: string;
  node_id?: string | null;
  hint_key?: string | null;
}

/**
 * The diagnostics of a refusal, read from the error envelope's `details`.
 * Tolerant by design: a store refusal, or any response that predates
 * aggregation, carries none and the caller falls back to the message alone.
 */
export function parseWorkflowDiagnostics(error: unknown): WorkflowDiagnostic[] {
  const raw = parseApiError(error)?.details?.diagnostics;
  if (!Array.isArray(raw)) return [];
  const diagnostics: WorkflowDiagnostic[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) continue;
    const candidate = item as Record<string, unknown>;
    if (
      typeof candidate.code !== "string" ||
      typeof candidate.message !== "string"
    ) {
      continue;
    }
    diagnostics.push({
      code: candidate.code,
      message: candidate.message,
      path: typeof candidate.path === "string" ? candidate.path : undefined,
      node_id:
        typeof candidate.node_id === "string" ? candidate.node_id : undefined,
      hint_key:
        typeof candidate.hint_key === "string" ? candidate.hint_key : undefined,
    });
  }
  return diagnostics;
}

// --- definition metadata (derived from the one JSON Schema) -----------------

/** One form field: its JSON type, whether it is required, and its bounds. */
export interface WorkflowFieldMetadata {
  name: string;
  type: string | string[];
  required: boolean;
  enum?: unknown[];
  const?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minProperties?: number;
  maxProperties?: number;
  pattern?: string;
  format?: string;
  default?: unknown;
  items?: Record<string, unknown>;
  fields?: WorkflowFieldMetadata[];
}

export interface WorkflowFieldBlock {
  required: string[];
  optional: string[];
  fields: WorkflowFieldMetadata[];
}

export interface WorkflowNodeTypeMetadata {
  type: string;
  required: string[];
  optional: string[];
  config_fields: WorkflowFieldMetadata[];
  /** Config fields where `{{ … }}` placeholders are read. */
  template_fields: string[];
}

export interface WorkflowTriggerTypeMetadata extends WorkflowFieldBlock {
  type: string;
}

export interface WorkflowReferenceSyntax {
  identifier: { pattern: string | null };
  template: {
    open: string;
    close: string;
    examples: string[];
    max_placeholders: number;
    fields: Record<string, string>;
  };
  references: { kind: string; syntax: string }[];
  max_reference_length: number;
}

/** `GET /workflow-definitions/metadata`: the contract an editor builds forms from. */
export interface WorkflowDefinitionMetadata {
  schema_version: number;
  compiler_version: string;
  node_types: WorkflowNodeTypeMetadata[];
  node: WorkflowFieldBlock;
  inputs: WorkflowFieldBlock & { types: string[] };
  edges: WorkflowFieldBlock;
  trigger_types: WorkflowTriggerTypeMetadata[];
  limits: WorkflowFieldBlock;
  output: WorkflowFieldBlock;
  reference_syntax: WorkflowReferenceSyntax;
  cel_reference_namespaces: {
    namespace: string;
    kind: string;
    syntax: string;
  }[];
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

  /**
   * Describe a workflow and get a draft back. The server runs the authoring loop
   * (restricted document → lowering → compiler, at most two rounds) and returns
   * the explanation; a description that never compiles comes back as an error
   * whose diagnostics say what the compiler still objected to.
   */
  authorWorkflowDraft: (body: WorkflowAuthoringRequest) =>
    unwrap<AuthoredWorkflow>(`${BASE}/workflow-authoring`, jsonInit("POST", body)),
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
  /**
   * Definition-level comparison of two versions of the same workflow. The
   * server answers with the keyed semantic diff, so a node reorder alone is
   * not reported as a change; the payload is parsed tolerantly because a
   * missing change list must not read as "no differences".
   */
  compareWorkflowVersions: (
    id: string,
    fromVersionId: string,
    toVersionId: string,
  ) =>
    unwrap<unknown>(
      withQuery(`${BASE}/workflows/${encodeURIComponent(id)}/versions/diff`, {
        from: fromVersionId,
        to: toVersionId,
      }),
    ).then(parseWorkflowVersionDiff),

  // Definition validation — canonicalizes a draft without persisting it.
  validateDefinition: (definition: JsonObject) =>
    unwrap<DefinitionValidation>(
      `${BASE}/workflow-definitions/validate`,
      jsonInit("POST", { definition }),
    ),

  // Definition metadata — the compiler contract a form is rendered from.
  getDefinitionMetadata: () =>
    unwrap<WorkflowDefinitionMetadata>(`${BASE}/workflow-definitions/metadata`),

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
