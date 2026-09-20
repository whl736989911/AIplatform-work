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
 *   GET  /v1/input-requests                GET /v1/executions/{id}/input-requests
 *   POST /v1/executions/{id}/input-requests/{input_request_id}/answer
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
  // Migration 034 added ``waiting_input`` to both CHECK constraints: the run is
  // alive and parked on a question (``ask`` node) rather than on a decision.
  "waiting_input",
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

/**
 * Step-run states, mirroring ``workbuddy_step_runs_status_check`` as migration
 * 023 restated it from the published contract. Migration 018 had invented
 * ``succeeded`` / ``cancelled`` and had no ``waiting_reconciliation``, so a step
 * parked on an unknown external write had no state to occupy.
 */
export const STEP_STATUSES = [
  "queued",
  "running",
  "waiting_approval",
  "waiting_reconciliation",
  "success",
  "failed",
  "skipped",
  "canceled",
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];

/** Why a skipped step produced nothing (mirrors the step_runs CHECK constraint). */
export const STEP_SKIP_REASONS = ["not_selected", "upstream_failed"] as const;
export type StepSkipReason = (typeof STEP_SKIP_REASONS)[number];

export const WORKFLOW_NODE_TYPES = [
  "tool",
  "llm",
  "condition",
  "approval",
  "transform",
  // 显式节点（A-07）：输入、检索、输出各自成为图里的一步
  "input",
  "knowledge",
  "output",
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

/**
 * The only outcomes an operator may record for a parked external write; the
 * step either produced the write (``confirmed_success``) or it did not
 * (``confirmed_failed``). An undecidable outcome is not a row at all.
 */
export const RECONCILIATION_DECISIONS = [
  "confirmed_success",
  "confirmed_failed",
] as const;
export type ReconciliationDecision =
  (typeof RECONCILIATION_DECISIONS)[number];

/** Row visibility filter shared by the execution and approval list routes. */
export const WORKBUDDY_SCOPES = ["self", "tenant"] as const;
export type WorkBuddyScope = (typeof WORKBUDDY_SCOPES)[number];

// --- executions ------------------------------------------------------------

/**
 * The token counters a model adapter reported for one step, stored verbatim:
 * the keys are the adapter's own, so the console reads ``total_tokens`` /
 * ``input_tokens`` / ``output_tokens`` and tolerates the OpenAI
 * ``prompt_tokens`` / ``completion_tokens`` aliases. A deployment whose adapter
 * reported nothing sends ``null`` here instead of a zero-filled mapping.
 */
export type TokenUsage = Record<string, unknown>;

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
  /**
   * The activation recorded when the node was dispatched: the run payloads, the
   * resolved node definition and the input this node was handed. ``null`` for a
   * step that never dispatched (skipped, or decided without running).
   */
  input?: JsonObject | null;
  /** Whatever the node recorded — any JSON value, not only an object. */
  output?: unknown;
  /** Step time the engine measured; ``null`` while the step has not finished. */
  duration_ms?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** Set on ``skipped``, the only status the server attaches a reason to. */
  skip_reason?: StepSkipReason | null;
  /** Adapter-reported usage: model nodes only, ``null`` on every other type. */
  token_usage?: TokenUsage | null;
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
  /**
   * Milliseconds the run spent inside its steps. The server sums every attempt's
   * measured step time, so approval and reconciliation waits are excluded; ``0``
   * means nothing was measured yet.
   */
  active_duration_ms?: number | null;
  /**
   * Total tokens the run's model adapters reported, as the runtime keeps it: one
   * integer for the execution (``workbuddy_executions.token_usage``, migration
   * 025), not the per-node mapping a step carries. ``0`` means nothing was
   * reported.
   */
  token_usage?: number | null;
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

/** One recorded decision, as ``GET /v1/executions/{id}/reconciliations`` answers. */
export interface Reconciliation {
  id: string;
  /** The step run that was settled. The list route returns no node id with it. */
  step_run_id: string;
  decision: ReconciliationDecision;
  /** The evidence payload the caller submitted — by reference, never inline. */
  evidence_ref: string;
  evidence_hash: string;
  external_request_id: string | null;
  /** The payload holding the verified result; set on ``confirmed_success`` only. */
  result_payload_ref: string | null;
  /** The operator's reason, stored verbatim. */
  note: string;
  decided_by_user_id: number | null;
  /** ISO-8601. */
  created_at: string | null;
}

/**
 * One decision about an unknown external write.
 *
 * ``step_id`` is the node id of the step being settled — the route resolves it
 * with ``find_step_run(..., node_id, status="waiting_reconciliation")`` and
 * refuses any other step. ``evidence_ref`` must already be a payload of this
 * execution, so the caller submits a reference, not a payload.
 */
export interface ReconciliationCreateRequest {
  step_id: string;
  decision: ReconciliationDecision;
  evidence_ref: string;
  /** Required: the server rejects a decision without the operator's reason. */
  reason: string;
  external_reference?: string | null;
}

/** The record response: the decision, plus the execution it moved. */
export interface ReconciliationRecorded {
  id: string;
  execution_id: string;
  node_id: string;
  step_run_id: string;
  decision: ReconciliationDecision;
  external_reference: string | null;
  evidence_ref: string;
  evidence_hash: string;
  result_payload_ref: string | null;
  /** How many decisions this execution carries now. */
  reconciliations: number;
  /** The execution after the decision: re-queued, or canceled if it was asked for. */
  execution: Execution;
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

// --- input requests --------------------------------------------------------

export const INPUT_REQUEST_STATUSES = [
  "open",
  "submitted",
  "expired",
  "invalidated",
] as const;
export type InputRequestStatus = (typeof INPUT_REQUEST_STATUSES)[number];

/** The kinds of answer a question may declare (mirrors ``ASK_FIELD_TYPES``). */
export const INPUT_FIELD_TYPES = [
  "string",
  "text",
  "integer",
  "number",
  "boolean",
  "date",
  "select",
] as const;
export type InputFieldType = (typeof INPUT_FIELD_TYPES)[number];

export const INPUT_ASSIGNEE_STATUSES = [
  "pending",
  "answered",
  "abstained",
  "invalidated",
] as const;
export type InputAssigneeStatus = (typeof INPUT_ASSIGNEE_STATUSES)[number];

/** One field of a question: what to render, and what the answer must satisfy. */
export interface InputField {
  name: string;
  label: string;
  type: InputFieldType;
  /** Absent means required — the server applies ``required: true`` by default. */
  required?: boolean;
  placeholder?: string;
  /** The only values a ``select`` accepts. */
  options?: string[];
}

/**
 * The form a question was asked with, stored beside the asking node's own
 * snapshot (node name, assignees, timeout). ``fields`` is the part an answer is
 * checked against; the other keys are that node's context.
 */
export interface InputForm {
  fields: InputField[];
}

/** Who may answer a question, and whether they already did. */
export interface InputAssignee {
  user_id: number;
  department_id: string | null;
  status: InputAssigneeStatus;
  submitted_at: string | null;
}

/** One question a run is parked on, as both list routes answer it. */
export interface InputRequest {
  id: string;
  execution_id: string;
  node_id: string;
  status: InputRequestStatus;
  prompt: string;
  form: InputForm;
  /** The answer that was submitted; ``null`` while the question is open. */
  values: JsonObject | null;
  expires_at: string | null;
  submitted_by_user_id: number | null;
  submitted_at: string | null;
  created_at: string;
  assignees: InputAssignee[];
}

/**
 * An answer to one question: exactly the form's declared field names. A key the
 * form does not declare is refused, so the UI sends only what it rendered.
 */
export interface InputAnswerRequest {
  values: JsonObject;
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

export interface InputRequestListQuery {
  scope?: WorkBuddyScope;
  status?: InputRequestStatus | "";
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

  // Input requests — the questions a run asked, and the answers to them. Both
  // list routes answer with the form, because a client that shows a question
  // has to render the fields its answer will be checked against.
  listInputRequests: (query: InputRequestListQuery = {}) =>
    unwrapItems<InputRequest>(
      withQuery(`${BASE}/input-requests`, {
        scope: query.scope,
        status: query.status,
        limit: query.limit,
      }),
    ),
  listExecutionInputRequests: (executionId: string) =>
    unwrapItems<InputRequest>(
      `${BASE}/executions/${encodeURIComponent(executionId)}/input-requests`,
    ),
  answerInputRequest: (
    executionId: string,
    inputRequestId: string,
    body: InputAnswerRequest,
  ) =>
    unwrap<Execution>(
      `${BASE}/executions/${encodeURIComponent(executionId)}/input-requests/${encodeURIComponent(inputRequestId)}/answer`,
      jsonInit("POST", body),
    ),

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
