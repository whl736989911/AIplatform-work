/**
 * WorkBuddy improvement-proposal API — all paths are relative to ``/api/v1``
 * (``request()`` adds the ``/api`` prefix).
 *
 * Frozen manifest (contracts/route-manifest.json):
 *   POST /v1/workflows/{workflow_id}/improvement-proposals   (202, stage B)
 *   GET  /v1/improvement-proposals
 *   GET  /v1/improvement-proposals/{id}
 *   POST /v1/improvement-proposals/{id}/decisions
 *   POST /v1/improvement-proposals/{id}/promote              (If-Match + ETag)
 *   POST /v1/improvement-proposals/{id}/reviewers            (stage B1)
 *
 * Field names come from ``octop.infra.workbuddy.proposals`` (``ProposalView``,
 * the review/shadow/gate projections) and from the bodies the router on
 * ``feat/workbuddy-b-proposals`` validates. The reviewer-assignment route has
 * no implementation yet in any branch, so its body is the one shape the frozen
 * manifest does not pin: it follows the slice's ``reviewer_membership_id``
 * vocabulary, and the page surfaces whatever the server answers (today a 404)
 * instead of pretending the assignment happened.
 */

import { request } from "../request";

const BASE = "/v1";

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

function jsonInit(
  method: string,
  body: unknown,
  headers?: HeadersInit,
): RequestInit {
  return { method, headers, body: JSON.stringify(body) };
}

/**
 * True when the proposals slice is not reachable from this deployment: an
 * unmounted router answers 404, an unimplemented one 501, and a missing
 * control-plane database 503. Callers render an explicit "not available yet"
 * state for these — never fabricated proposals or a silent empty list.
 */
export function isProposalsUnavailableError(error: unknown): boolean {
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

// --- Proposal projection ---------------------------------------------------

export type ProposalStatus =
  | "under_review"
  | "approved"
  | "rejected"
  | "shadow"
  | "canary"
  | "applied"
  | "aborted"
  | "superseded"
  | "stale";

export type ProposalRiskLevel = "low" | "medium" | "high";

export type ProposalChangeKind = "added" | "removed" | "replaced";

/** One node-level difference between the base and the candidate definition. */
export interface ProposalSemanticChange {
  path: string;
  kind: ProposalChangeKind | string;
  old: unknown;
  new: unknown;
}

export type ProposalReviewDecision = "approved" | "rejected";

export interface ProposalReview {
  review_id: string;
  reviewer_user_id: number;
  decision: ProposalReviewDecision | string;
  comment: string;
  created_at: number;
}

/** Replay-only shadow evidence; ``failures`` names why the proof is short. */
export interface ProposalShadowProof {
  complete: boolean;
  settled_runs: number;
  failures: string[];
}

export interface ProposalPhaseMetrics {
  settled_runs: number;
  success_rate: number;
  p95_latency_ms: number;
  avg_tokens: number;
  safety_violations: number;
}

/** Canary gate verdict; ``failures`` are the unmet gate names. */
export interface ProposalGateVerdict {
  passed: boolean;
  failures: string[];
  safety_stop: boolean;
  full_days: number;
}

export interface ProposalEvaluation {
  evaluation_id: string;
  phase: "shadow" | "canary" | string;
  window_start: number;
  window_end: number;
  verdict: ProposalGateVerdict;
  baseline: ProposalPhaseMetrics;
  candidate: ProposalPhaseMetrics;
}

/**
 * Detail projection. ``reviews`` is absent for non-admin readers (the router
 * strips it), and the optional evidence blocks appear only once they exist.
 */
export interface ImprovementProposal {
  proposal_id: string;
  workflow_id: string;
  workflow_revision: number;
  base_version_id: string;
  base_content_hash: string;
  candidate_version_id: string;
  candidate_content_hash: string;
  status: ProposalStatus | string;
  status_reason: string | null;
  risk_level: ProposalRiskLevel | string;
  pii_involved: boolean;
  required_approvals: number;
  requires_manual_shadow: boolean;
  change_summary: string;
  changes: ProposalSemanticChange[];
  created_by_user_id: number;
  created_at: number;
  updated_at: number;
  canary_ratio_bp: number | null;
  canary_started_at: number | null;
  canary_stopped_at: number | null;
  canary_stop_reason: string | null;
  applied_version_id: string | null;
  stale: boolean;
  reviews?: ProposalReview[];
  shadow_proof?: ProposalShadowProof;
  evaluations?: ProposalEvaluation[];
  last_gate?: ProposalGateVerdict;
}

// --- Requests --------------------------------------------------------------

/** RFC 6902 operation; the patch is applied to the server-fixed base. */
export interface ProposalPatchOperation {
  op: "add" | "remove" | "replace" | "move" | "copy" | "test";
  path: string;
  value?: unknown;
  from?: string;
}

export interface ProposalCreateRequest {
  /** Workflow revision the patch was drafted from — the server fixes the base. */
  workflow_revision: number;
  patch: ProposalPatchOperation[];
  change_summary?: string;
}

/** 202 body of the create route: the job and the risk class it compiled to. */
export interface ProposalCreateResult {
  job_id: string;
  proposal_id: string;
  workflow_id: string;
  status: ProposalStatus | string;
  risk_level: ProposalRiskLevel | string;
  required_approvals: number;
  requires_manual_shadow: boolean;
}

export interface ProposalDecisionRequest {
  decision: ProposalReviewDecision;
  comment?: string;
}

export type ProposalPromotionAction =
  | "start_shadow"
  | "start_canary"
  | "apply"
  | "abort";

export interface ProposalPromoteRequest {
  action: ProposalPromotionAction;
  /** Candidate share for ``start_canary``, in basis points (1..10000). */
  ratio_basis_points?: number;
}

/**
 * Promote response: the proposal view with ``workflow_revision`` carrying the
 * *resulting* workflow revision (the route rewrites it after the CAS) and the
 * same revision mirrored in the ``ETag`` response header.
 */
export type ProposalPromoteResult = ImprovementProposal;

export interface ProposalListQuery {
  workflow_id?: string;
  status?: ProposalStatus;
  limit?: number;
}

export interface ProposalListResult {
  items: ImprovementProposal[];
}

/**
 * Stage B1 body. The route is frozen in the manifest ("assign an independent
 * reviewer; tenant admins; active and not the creator") but is not implemented
 * anywhere yet, so this is the one shape the wire has not confirmed.
 */
export interface ProposalReviewersRequest {
  reviewer_membership_ids: string[];
}

export const workbuddyProposalsApi = {
  /** 202: compiles the patch against the current base and opens the proposal. */
  createProposal: (workflowId: string, body: ProposalCreateRequest) =>
    unwrap<ProposalCreateResult>(
      `${BASE}/workflows/${encodeURIComponent(
        workflowId,
      )}/improvement-proposals`,
      jsonInit("POST", body),
    ),

  listProposals: (query: ProposalListQuery = {}) => {
    const params = new URLSearchParams();
    if (query.workflow_id?.trim())
      params.set("workflow_id", query.workflow_id.trim());
    if (query.status) params.set("status", query.status);
    if (typeof query.limit === "number")
      params.set("limit", String(query.limit));
    const suffix = params.toString();
    return unwrap<ProposalListResult | ImprovementProposal[]>(
      `${BASE}/improvement-proposals${suffix ? `?${suffix}` : ""}`,
    ).then((body): ImprovementProposal[] =>
      Array.isArray(body) ? body : body.items,
    );
  },

  getProposal: (proposalId: string) =>
    unwrap<ImprovementProposal>(
      `${BASE}/improvement-proposals/${encodeURIComponent(proposalId)}`,
    ),

  /** Independent review; the creator can never vote and rejects win. */
  decideProposal: (proposalId: string, body: ProposalDecisionRequest) =>
    unwrap<ImprovementProposal>(
      `${BASE}/improvement-proposals/${encodeURIComponent(
        proposalId,
      )}/decisions`,
      jsonInit("POST", body),
    ),

  /**
   * Promote with compare-and-swap: ``If-Match`` carries the workflow revision
   * the caller last read, and the response ETag is the resulting revision.
   */
  promoteProposal: (
    proposalId: string,
    body: ProposalPromoteRequest,
    workflowRevision: number,
  ) =>
    unwrap<ProposalPromoteResult>(
      `${BASE}/improvement-proposals/${encodeURIComponent(proposalId)}/promote`,
      jsonInit("POST", body, { "If-Match": `W/"${workflowRevision}"` }),
    ),

  assignReviewers: (proposalId: string, body: ProposalReviewersRequest) =>
    unwrap<ImprovementProposal>(
      `${BASE}/improvement-proposals/${encodeURIComponent(
        proposalId,
      )}/reviewers`,
      jsonInit("POST", body),
    ),
};
