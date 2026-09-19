/**
 * WorkBuddy runtime API: executions, approvals, reconciliation, notifications —
 * paths are relative to ``/api/v1`` (``request()`` adds the ``/api`` prefix).
 *
 * Manifest (contracts/route-manifest.json, stage A3):
 *   POST /v1/workflows/{id}/execute        GET /v1/executions
 *   GET  /v1/executions/{id}               POST /v1/executions/{id}/resume
 *   POST /v1/executions/{id}/cancel        POST /v1/executions/{id}/reconciliations
 *   GET  /v1/executions/{id}/reconciliations
 *   GET  /v1/approval-requests             GET /v1/approval-requests/{id}
 *   POST /v1/approval-requests/{id}/challenge
 *   GET  /v1/notifications                 POST /v1/notifications/{id}/read
 *
 * Two invariants this module never breaks:
 *  - A resumed execution is a decision on a *pending* approval and is guarded by
 *    a two-minute one-time challenge. ``challengeApprovalRequest`` returns that
 *    token exactly once (the server keeps only its hash); it lives in the
 *    caller's modal state, is never persisted and is never logged.
 *  - The challenge response is the one body whose token sits *beside* ``data``
 *    rather than inside it, so it is unwrapped explicitly here.
 */

import { request } from "../request";
import {
  isWorkBuddyUnavailableError,
  type ApiEnvelope,
  type JsonObject,
} from "./workbuddyWorkflows";

const BASE = "/v1";

/** Re-exported so callers of this slice need a single import for the state check. */
export { isWorkBuddyUnavailableError };

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

/** All list routes answer ``{ items }``. */
async function unwrapItems<T>(path: string, init?: RequestInit): Promise<T[]> {
  const body = await unwrap<{ items: T[] }>(path, init);
  return body.items;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return body === undefined
    ? { method }
    : { method, body: JSON.stringify(body) };
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

// --- vocabularies (mirror the PostgreSQL CHECK constraints) ----------------

export const EXECUTION_STATUSES = [
  "pending",
  "running",
  "waiting_approval",
  "succeeded",
  "failed",
  "partial",
  "cancelled",
] as const;
export type ExecutionStatus = (typeof EXECUTION_STATUSES)[number];

/** Statuses the server still accepts a cancel for. */
export const CANCELLABLE_EXECUTION_STATUSES: readonly ExecutionStatus[] = [
  "pending",
  "running",
  "waiting_approval",
];

export const EXECUTION_TRIGGER_TYPES = [
  "manual",
  "cron",
  "webhook",
  "event",
  "api",
] as const;
export type ExecutionTriggerType = (typeof EXECUTION_TRIGGER_TYPES)[number];

export const STEP_STATUSES = [
  "running",
  "succeeded",
  "failed",
  "skipped",
  "waiting_approval",
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];

export const WORKFLOW_NODE_TYPES = [
  "tool",
  "llm",
  "condition",
  "approval",
  "transform",
] as const;
export type WorkflowNodeType = (typeof WORKFLOW_NODE_TYPES)[number];

export const APPROVAL_STATUSES = [
  "pending",
  "approved",
  "rejected",
  "expired",
  "invalidated",
] as const;
export type ApprovalRequestStatus = (typeof APPROVAL_STATUSES)[number];

export const APPROVAL_CANDIDATE_STATUSES = [
  "pending",
  "approved",
  "rejected",
  "abstained",
  "invalidated",
] as const;
export type ApprovalCandidateStatus =
  (typeof APPROVAL_CANDIDATE_STATUSES)[number];

export const APPROVAL_DECISIONS = ["approve", "reject"] as const;
export type ApprovalDecision = (typeof APPROVAL_DECISIONS)[number];

export const RECONCILIATION_STATUSES = [
  "pending",
  "matched",
  "mismatched",
  "unresolved",
] as const;
export type ReconciliationStatus = (typeof RECONCILIATION_STATUSES)[number];

/** Row visibility filter shared by the execution and approval list routes. */
export const WORKBUDDY_SCOPES = ["self", "tenant"] as const;
export type WorkBuddyScope = (typeof WORKBUDDY_SCOPES)[number];

// --- executions ------------------------------------------------------------

export interface Execution {
  id: string;
  workflow_id: string;
  workflow_version_id: string;
  status: ExecutionStatus;
  trigger_type: ExecutionTriggerType;
  inputs: JsonObject;
  outputs: JsonObject;
  error_code: string | null;
  error_message: string | null;
  created_by_user_id: number | null;
  /** ISO-8601. */
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ExecutionStep {
  node_id: string;
  node_type: WorkflowNodeType;
  attempt: number;
  status: StepStatus;
  save_as: string | null;
  error_code: string | null;
}

export interface ExecutionEdge {
  from: string;
  to: string;
  branch: string | null;
  taken: boolean;
}

export interface ExecutionDetail extends Execution {
  steps: ExecutionStep[];
  edges: ExecutionEdge[];
}

export interface ExecutionCreateRequest {
  inputs?: JsonObject;
  /** Replay key: the same key with the same request returns the same execution. */
  idempotency_key?: string | null;
}

export interface ExecutionResumeRequest {
  approval_request_id: string;
  decision: ApprovalDecision;
  token: string;
}

export interface Reconciliation {
  id: string;
  node_id: string;
  status: ReconciliationStatus;
  external_ref: string | null;
  evidence_sha256: string;
  /** ISO-8601. */
  created_at: string | null;
  resolved_at: string | null;
}

export interface ReconciliationCreateRequest {
  node_id: string;
  status: ReconciliationStatus;
  evidence?: JsonObject;
  external_ref?: string | null;
}

export interface ReconciliationRecorded {
  id: string;
  execution_id: string;
  node_id: string;
  status: ReconciliationStatus;
  external_ref: string | null;
  evidence_sha256: string;
  reconciliations: number;
}

// --- approvals -------------------------------------------------------------

export interface ApprovalRequest {
  id: string;
  execution_id: string;
  node_id: string;
  status: ApprovalRequestStatus;
  required_approvals: number;
  decided_approvals: number;
  /** The frozen parameter snapshot this decision applies to. */
  params: JsonObject;
  decision: ApprovalDecision | null;
  decided_at: string | null;
  created_at: string | null;
  /** When the currently issued challenge stops being accepted. */
  token_expires_at: string | null;
}

export interface ApprovalCandidate {
  user_id: number;
  status: ApprovalCandidateStatus;
}

export interface ApprovalRequestDetail extends ApprovalRequest {
  candidates: ApprovalCandidate[];
}

/** One-time challenge: the raw token is returned once and never stored here. */
export interface ApprovalChallenge {
  approval_request_id: string;
  token: string;
  /** Validity window in seconds (two minutes on the server). */
  expires_in: number;
}

// --- notifications ---------------------------------------------------------

export interface WorkBuddyNotification {
  id: string;
  /** Free-form server vocabulary (approval, execution, job, …). */
  kind: string;
  title: string;
  body: string | null;
  resource_type: string | null;
  resource_id: string | null;
  read: boolean;
  created_at: string | null;
}

// --- queries ---------------------------------------------------------------

export interface ExecutionListQuery {
  scope?: WorkBuddyScope;
  workflow_id?: string;
  status?: ExecutionStatus | "";
  limit?: number;
}

export interface ApprovalListQuery {
  scope?: WorkBuddyScope;
  status?: ApprovalRequestStatus | "";
  limit?: number;
}

export interface NotificationListQuery {
  unread_only?: boolean;
  limit?: number;
}

export const workbuddyRuntimeApi = {
  // Executions — the run itself is accepted asynchronously (202).
  executeWorkflow: (workflowId: string, body: ExecutionCreateRequest = {}) =>
    unwrap<Execution>(
      `${BASE}/workflows/${encodeURIComponent(workflowId)}/execute`,
      jsonInit("POST", body),
    ),
  listExecutions: (query: ExecutionListQuery = {}) =>
    unwrapItems<Execution>(
      withQuery(`${BASE}/executions`, {
        scope: query.scope,
        workflow_id: query.workflow_id,
        status: query.status,
        limit: query.limit,
      }),
    ),
  getExecution: (id: string) =>
    unwrap<ExecutionDetail>(`${BASE}/executions/${encodeURIComponent(id)}`),
  cancelExecution: (id: string) =>
    unwrap<Execution>(
      `${BASE}/executions/${encodeURIComponent(id)}/cancel`,
      jsonInit("POST"),
    ),
  resumeExecution: (id: string, body: ExecutionResumeRequest) =>
    unwrap<Execution>(
      `${BASE}/executions/${encodeURIComponent(id)}/resume`,
      jsonInit("POST", body),
    ),
  listReconciliations: (executionId: string) =>
    unwrapItems<Reconciliation>(
      `${BASE}/executions/${encodeURIComponent(executionId)}/reconciliations`,
    ),
  recordReconciliation: (
    executionId: string,
    body: ReconciliationCreateRequest,
  ) =>
    unwrap<ReconciliationRecorded>(
      `${BASE}/executions/${encodeURIComponent(executionId)}/reconciliations`,
      jsonInit("POST", body),
    ),

  // Approval requests — the inbox of the current approver.
  listApprovalRequests: (query: ApprovalListQuery = {}) =>
    unwrapItems<ApprovalRequest>(
      withQuery(`${BASE}/approval-requests`, {
        scope: query.scope,
        status: query.status,
        limit: query.limit,
      }),
    ),
  getApprovalRequest: (id: string) =>
    unwrap<ApprovalRequestDetail>(
      `${BASE}/approval-requests/${encodeURIComponent(id)}`,
    ),
  challengeApprovalRequest: async (id: string): Promise<ApprovalChallenge> => {
    const body = await request<
      ApiEnvelope<{ approval_request_id: string }> & {
        token: string;
        expires_in: number;
      }
    >(
      `${BASE}/approval-requests/${encodeURIComponent(id)}/challenge`,
      jsonInit("POST"),
    );
    return {
      approval_request_id: body.data.approval_request_id,
      token: body.token,
      expires_in: body.expires_in,
    };
  },

  // Notifications — the caller's own rows only.
  listNotifications: (query: NotificationListQuery = {}) =>
    unwrapItems<WorkBuddyNotification>(
      withQuery(`${BASE}/notifications`, {
        unread_only: query.unread_only,
        limit: query.limit,
      }),
    ),
  readNotification: (id: string) =>
    unwrap<WorkBuddyNotification>(
      `${BASE}/notifications/${encodeURIComponent(id)}/read`,
      jsonInit("POST"),
    ),
};
