"""Improvement proposals: patch safety, review gates, promotion and canary routing."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from octop.infra.workbuddy import proposals as P

APPROVER = "11111111-1111-1111-1111-111111111111"
POLICY = P.ProposalPolicy(
    approved_tools=frozenset({"safe_tool", "other_tool"}), private_ids=frozenset({"private-42"})
)
WORKFLOW_ID = "workflow-1"
INITIAL_REVISION = 7


def workflow_definition(*, prompt: str = "summarise", pii: bool = False) -> dict[str, Any]:
    parameters: dict[str, Any] = {"channel": "reports"}
    if pii:
        parameters["data_classification"] = "pii"
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {},
        "nodes": [
            {
                "id": "start",
                "type": "llm",
                "name": "Start",
                "config": {"prompt": prompt, "model": "model-a"},
            },
            {
                "id": "fetch",
                "type": "tool",
                "name": "Fetch",
                "config": {"tool_name": "safe_tool", "parameters": parameters},
            },
            {
                "id": "gate",
                "type": "approval",
                "name": "Gate",
                "config": {"approval_message": "approve?", "approver_user_ids": [APPROVER]},
            },
            {
                "id": "end",
                "type": "transform",
                "name": "End",
                "config": {"input": {}, "expression": "1"},
            },
        ],
        "edges": [
            {"from": "start", "to": "fetch"},
            {"from": "fetch", "to": "gate"},
            {"from": "gate", "to": "end"},
        ],
        "limits": {},
        "output": {"format": "json", "destination": "user"},
    }


def compile_ok(
    patch: Sequence[Mapping[str, Any]], *, base: Mapping[str, Any] | None = None
) -> P.CompiledProposal:
    return P.compile_proposal(
        base if base is not None else workflow_definition(), patch, policy=POLICY
    )


def rejection(patch: Sequence[Mapping[str, Any]], *, base: Mapping[str, Any] | None = None) -> str:
    with pytest.raises(P.ProposalPolicyError) as caught:
        compile_ok(patch, base=base)
    return caught.value.code


# --------------------------------------------------------------------------- #
# RFC 6902 application
# --------------------------------------------------------------------------- #


def test_patch_applies_rfc6902_operations() -> None:
    document = {"a": [1, 2, 3], "b": {"x": "y"}}
    operations = P.parse_patch(
        [
            {"op": "add", "path": "/a/-", "value": 4},
            {"op": "replace", "path": "/b/x", "value": "z"},
            {"op": "remove", "path": "/a/0"},
            {"op": "copy", "from": "/b/x", "path": "/c"},
            {"op": "move", "from": "/a/0", "path": "/a/2"},
            {"op": "test", "path": "/c", "value": "z"},
        ]
    )

    assert P.apply_patch(document, operations) == {"a": [3, 4, 2], "b": {"x": "z"}, "c": "z"}
    assert document == {"a": [1, 2, 3], "b": {"x": "y"}}  # never mutated


def test_pointer_decodes_escapes() -> None:
    operations = P.parse_patch([{"op": "replace", "path": "/a~1b/c~0d", "value": 2}])

    assert P.apply_patch({"a/b": {"c~d": 1}}, operations) == {"a/b": {"c~d": 2}}


def test_patch_cannot_target_document_root() -> None:
    with pytest.raises(P.PatchError) as caught:
        P.apply_patch({"a": 1}, P.parse_patch([{"op": "replace", "path": "", "value": {}}]))

    assert caught.value.code == "invalid_pointer"


def test_patch_rejects_leading_zero_indices_and_move_into_child() -> None:
    with pytest.raises(P.PatchError):
        P.apply_patch({"a": [1]}, P.parse_patch([{"op": "add", "path": "/a/01", "value": 2}]))
    with pytest.raises(P.PatchError):
        P.parse_patch([{"op": "move", "from": "/a", "path": "/a/0"}])


def test_test_operation_fails_against_changed_value() -> None:
    with pytest.raises(P.PatchError) as caught:
        P.apply_patch({"a": 1}, P.parse_patch([{"op": "test", "path": "/a", "value": 2}]))

    assert caught.value.code == "test_failed"


# --------------------------------------------------------------------------- #
# The final semantic diff is the authority
# --------------------------------------------------------------------------- #


def test_node_reorder_alone_is_not_a_semantic_change() -> None:
    base = workflow_definition()
    reordered = list(reversed(base["nodes"]))

    assert rejection([{"op": "replace", "path": "/nodes", "value": reordered}], base=base) == (
        "NO_SEMANTIC_CHANGE"
    )


def test_index_shift_cannot_hide_an_approver_change() -> None:
    # Move the approval node to the front and edit its approvers at the new
    # index: a policy keyed on patch indices would inspect the wrong node.
    patch = [
        {"op": "move", "from": "/nodes/2", "path": "/nodes/0"},
        {
            "op": "add",
            "path": "/nodes/0/config/approver_user_ids/-",
            "value": "22222222-2222-2222-2222-222222222222",
        },
    ]

    assert rejection(patch) == "APPROVER_CHANGE"


def test_whole_node_array_replacement_cannot_drop_approval() -> None:
    base = workflow_definition()
    reduced = [node for node in base["nodes"] if node["id"] != "gate"]

    assert (
        rejection([{"op": "replace", "path": "/nodes", "value": reduced}], base=base)
        == "APPROVAL_BYPASS"
    )


def test_rewiring_around_approval_node_is_rejected() -> None:
    patch = [
        {"op": "remove", "path": "/edges/0"},
        {"op": "add", "path": "/edges/-", "value": {"from": "start", "to": "end"}},
    ]

    assert rejection(patch) == "APPROVAL_BYPASS"


def test_valid_trigger_change_is_rejected() -> None:
    patch = [
        {
            "op": "replace",
            "path": "/trigger",
            "value": {"type": "event", "config": {"event_type": "upload"}},
        }
    ]

    assert rejection(patch) == "TRIGGER_CHANGE"


def test_auth_boundary_change_is_rejected() -> None:
    patch = [{"op": "add", "path": "/nodes/1/config/parameters/api_key", "value": "sk-live"}]

    assert rejection(patch) == "AUTH_BOUNDARY_CHANGE"


def test_target_change_is_rejected() -> None:
    patch = [{"op": "add", "path": "/nodes/1/config/parameters/connector_id", "value": "conn-1"}]

    assert rejection(patch) == "TARGET_CHANGE"


def test_unapproved_tool_is_rejected() -> None:
    patch = [{"op": "replace", "path": "/nodes/1/config/tool_name", "value": "unlisted_tool"}]

    assert rejection(patch) == "UNAPPROVED_TOOL"


def test_private_identifier_value_is_rejected() -> None:
    patch = [{"op": "add", "path": "/nodes/3/config/input/reference", "value": "private-42"}]

    assert rejection(patch) == "PRIVATE_ID"


def test_private_identifier_key_is_rejected() -> None:
    patch = [{"op": "add", "path": "/nodes/3/config/input/owner_id", "value": "someone"}]

    assert rejection(patch) == "PRIVATE_ID"


def test_missing_tool_allowlist_fails_closed() -> None:
    with pytest.raises(P.ProposalPolicyError) as caught:
        P.compile_proposal(
            workflow_definition(),
            [{"op": "add", "path": "/limits/max_steps", "value": 10}],
            policy=P.ProposalPolicy(),
        )

    assert caught.value.code == P.ALLOWLIST_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Risk classification and approvals
# --------------------------------------------------------------------------- #


def test_name_only_change_is_low_risk_single_approval() -> None:
    compiled = compile_ok([{"op": "replace", "path": "/nodes/0/name", "value": "Renamed"}])

    assert compiled.risk.level == "low"
    assert compiled.risk.pii is False
    assert compiled.risk.required_approvals == 1
    assert compiled.risk.requires_manual_shadow is False


def test_prompt_change_is_medium_risk_two_approvals_and_shadow() -> None:
    compiled = compile_ok(
        [{"op": "replace", "path": "/nodes/0/config/prompt", "value": "be terse"}]
    )

    assert compiled.risk.level == "medium"
    assert compiled.risk.required_approvals == 2
    assert compiled.risk.requires_manual_shadow is True


def test_tool_name_change_is_high_risk() -> None:
    compiled = compile_ok(
        [{"op": "replace", "path": "/nodes/1/config/tool_name", "value": "other_tool"}]
    )

    assert compiled.risk.level == "high"


def test_pii_definition_requires_two_approvals_and_shadow() -> None:
    compiled = compile_ok(
        [{"op": "replace", "path": "/nodes/0/name", "value": "Start2"}],
        base=workflow_definition(pii=True),
    )

    assert compiled.risk.pii is True
    assert compiled.risk.required_approvals == 2
    assert compiled.risk.requires_manual_shadow is True


def test_reject_wins_over_approvals() -> None:
    state = P.evaluate_approvals(
        [
            P.ReviewVote(2, P.ReviewDecision.APPROVED),
            P.ReviewVote(3, P.ReviewDecision.REJECTED),
            P.ReviewVote(4, P.ReviewDecision.APPROVED),
        ],
        creator_user_id=1,
        required_approvals=2,
    )

    assert state.outcome is P.ApprovalOutcome.REJECTED


def test_two_independent_approvals_are_required() -> None:
    pending = P.evaluate_approvals(
        [P.ReviewVote(2, P.ReviewDecision.APPROVED)], creator_user_id=1, required_approvals=2
    )
    approved = P.evaluate_approvals(
        [P.ReviewVote(2, P.ReviewDecision.APPROVED), P.ReviewVote(3, P.ReviewDecision.APPROVED)],
        creator_user_id=1,
        required_approvals=2,
    )

    assert pending.outcome is P.ApprovalOutcome.PENDING
    assert pending.remaining == 1
    assert approved.outcome is P.ApprovalOutcome.APPROVED


def test_creator_vote_is_refused() -> None:
    with pytest.raises(P.ProposalPolicyError) as caught:
        P.evaluate_approvals(
            [P.ReviewVote(1, P.ReviewDecision.APPROVED)], creator_user_id=1, required_approvals=1
        )

    assert caught.value.code == "CREATOR_SELF_REVIEW"


# --------------------------------------------------------------------------- #
# Canary bucketing (language-independent vectors)
# --------------------------------------------------------------------------- #

# int.from_bytes(sha256(utf-8)[0:8], "big") % 10000
CANARY_VECTORS = {
    "": 1652,
    "workbuddy": 8990,
    "tenant\x1fworkflow\x1frun-1": 3810,
    "00000000-0000-0000-0000-000000000000\x1f11111111-1111-1111-1111-111111111111\x1fexec-9": 9231,
}


def test_canary_bucket_matches_language_independent_vectors() -> None:
    for key, expected in CANARY_VECTORS.items():
        assert P.canary_bucket(key) == expected, key


def test_canary_bucket_spec_is_reproducible_without_this_module() -> None:
    key = "tenant\x1fworkflow\x1frun-1"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    manual = 0
    for byte in digest[:8]:
        manual = manual * 256 + byte

    assert manual % 10000 == P.canary_bucket(key)
    assert len(digest) == 32


def test_canary_lane_uses_basis_point_threshold() -> None:
    assert P.canary_lane("workbuddy", 0) == "baseline"
    assert P.canary_lane("workbuddy", 8991) == "candidate"
    assert P.canary_lane("workbuddy", 8990) == "baseline"
    assert P.canary_lane("workbuddy", 10000) == "candidate"


def test_canary_ratio_outside_range_is_rejected() -> None:
    with pytest.raises(P.ProposalPolicyError) as caught:
        P.is_canary_selected(1, 10001)

    assert caught.value.code == "CANARY_RATIO_INVALID"


# --------------------------------------------------------------------------- #
# Shadow proof and canary gates
# --------------------------------------------------------------------------- #


def shadow_runs(
    count: int, *, replay_only: bool = True, side_effects: int = 0, settled: bool = True
) -> list[P.ShadowRunRecord]:
    return [
        P.ShadowRunRecord(
            run_id=f"run-{index}",
            settled=settled,
            replay_only=replay_only,
            live_side_effects=side_effects,
        )
        for index in range(count)
    ]


def test_shadow_proof_requires_replay_only_and_no_live_side_effects() -> None:
    assert P.evaluate_shadow_proof(shadow_runs(P.SHADOW_MIN_SETTLED_RUNS)).complete is True
    assert P.evaluate_shadow_proof(shadow_runs(1)).failures == ("insufficient_shadow_runs",)
    assert P.evaluate_shadow_proof(
        shadow_runs(P.SHADOW_MIN_SETTLED_RUNS, replay_only=False)
    ).failures == ("shadow_not_replay_only",)
    assert P.evaluate_shadow_proof(
        shadow_runs(P.SHADOW_MIN_SETTLED_RUNS, side_effects=1)
    ).failures == ("shadow_live_side_effects",)
    assert P.evaluate_shadow_proof(shadow_runs(0)).failures == ("no_settled_shadow_runs",)


def canary_evidence(
    *,
    days: int = 8,
    baseline_runs: int = 500,
    candidate_runs: int = 300,
    success: tuple[float, float] = (0.99, 0.985),
    latency: tuple[float, float] = (1000.0, 1050.0),
    tokens: tuple[float, float] = (1000.0, 1010.0),
    safety_violations: int = 0,
) -> P.CanaryEvidence:
    return P.CanaryEvidence(
        window_start=1_700_000_000,
        window_end=1_700_000_000 + days * P.CANARY_FULL_DAY_SECONDS,
        baseline=P.PhaseMetrics(baseline_runs, success[0], latency[0], tokens[0]),
        candidate=P.PhaseMetrics(
            candidate_runs, success[1], latency[1], tokens[1], safety_violations
        ),
    )


def test_gates_pass_with_a_full_week_and_enough_samples() -> None:
    verdict = P.evaluate_canary_gates(canary_evidence())

    assert verdict.passed is True
    assert verdict.failures == ()
    assert verdict.full_days == 8


def test_insufficient_window_or_samples_never_passes() -> None:
    short = P.evaluate_canary_gates(canary_evidence(days=6))
    thin = P.evaluate_canary_gates(canary_evidence(candidate_runs=99))

    assert short.passed is False and "insufficient_window" in short.failures
    assert thin.passed is False and "insufficient_candidate_samples" in thin.failures


def test_quality_regressions_fail_the_gates() -> None:
    verdict = P.evaluate_canary_gates(
        canary_evidence(success=(0.99, 0.90), latency=(1000.0, 1500.0), tokens=(1000.0, 1400.0))
    )

    assert verdict.passed is False
    assert set(verdict.failures) == {
        "success_rate_below_floor",
        "success_rate_regression",
        "latency_regression",
        "token_regression",
    }


def test_safety_violation_sets_the_stop_flag() -> None:
    verdict = P.evaluate_canary_gates(canary_evidence(safety_violations=1))

    assert verdict.safety_stop is True
    assert "safety_violation" in verdict.failures


# --------------------------------------------------------------------------- #
# In-memory store with compare-and-swap semantics
# --------------------------------------------------------------------------- #


class FakeStore:
    def __init__(self, definition: Mapping[str, Any], *, revision: int = INITIAL_REVISION) -> None:
        self.definition = dict(definition)
        self.revision = revision
        self.hash = P.definition_hash(definition)
        self.proposals: dict[str, P.ProposalRecord] = {}
        self.reviews: dict[str, list[P.ReviewRecord]] = {}
        self.shadow: dict[str, list[P.ShadowRunRow]] = {}
        self.evaluations: dict[str, list[P.EvaluationRow]] = {}
        self.counter = 0

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    # -- creation ---------------------------------------------------------- #

    def create_proposal(self, request: P.NewProposal, *, compile: P.CompileFn) -> P.ProposalRecord:
        if request.expect_revision != self.revision:
            raise P.ProposalConflictError("STALE_REVISION", "workflow revision mismatch")
        if any(
            record.workflow_id == request.workflow_id and record.status in P.PENDING_STATUSES
            for record in self.proposals.values()
        ):
            raise P.ProposalConflictError(
                "PROPOSAL_ALREADY_OPEN", "workflow already has an open proposal"
            )
        compiled = compile(self.definition)
        record = P.ProposalRecord(
            proposal_id=self._id("proposal"),
            workflow_id=request.workflow_id,
            workflow_revision=self.revision,
            base_version_id="version-base",
            base_content_hash=compiled.base_content_hash,
            candidate_version_id=self._id("candidate"),
            candidate_content_hash=compiled.candidate_content_hash,
            status=P.ProposalStatus.UNDER_REVIEW,
            risk_level=compiled.risk.level,
            pii_involved=compiled.risk.pii,
            required_approvals=compiled.risk.required_approvals,
            requires_manual_shadow=compiled.risk.requires_manual_shadow,
            change_summary=request.change_summary,
            changes=compiled.changes,
            created_by_user_id=request.actor.user_id,
            created_by_membership_id=request.actor.membership_id,
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        )
        self.proposals[record.proposal_id] = record
        return record

    # -- reads ------------------------------------------------------------- #

    def get_proposal(self, proposal_id: str) -> P.ProposalRecord | None:
        return self.proposals.get(proposal_id)

    def list_proposals(
        self,
        *,
        workflow_id: str | None = None,
        status: P.ProposalStatus | None = None,
        limit: int = 100,
    ) -> list[P.ProposalRecord]:
        rows = [
            record
            for record in self.proposals.values()
            if (workflow_id is None or record.workflow_id == workflow_id)
            and (status is None or record.status is status)
        ]
        return sorted(rows, key=lambda row: row.created_at)[:limit]

    def list_reviews(self, proposal_id: str) -> list[P.ReviewRecord]:
        return list(self.reviews.get(proposal_id, ()))

    def add_review(self, review: P.NewReview) -> P.ReviewRecord:
        rows = self.reviews.setdefault(review.proposal_id, [])
        if any(row.reviewer_user_id == review.reviewer_user_id for row in rows):
            raise P.ProposalConflictError("DUPLICATE_REVIEW", "reviewer already voted")
        row = P.ReviewRecord(
            review_id=self._id("review"),
            proposal_id=review.proposal_id,
            reviewer_user_id=review.reviewer_user_id,
            reviewer_membership_id=review.reviewer_membership_id,
            decision=review.decision,
            comment=review.comment,
            created_at=review.created_at,
        )
        rows.append(row)
        return row

    def list_shadow_runs(self, proposal_id: str) -> list[P.ShadowRunRow]:
        return list(self.shadow.get(proposal_id, ()))

    def add_shadow_run(self, proposal_id: str, run: P.ShadowRunRow) -> P.ShadowRunRow:
        self.shadow.setdefault(proposal_id, []).append(run)
        return run

    def list_evaluations(self, proposal_id: str) -> list[P.EvaluationRow]:
        return list(self.evaluations.get(proposal_id, ()))

    def add_evaluation(
        self, proposal_id: str, evaluation: P.NewEvaluation, verdict: P.GateVerdict
    ) -> P.EvaluationRow:
        row = P.EvaluationRow(
            evaluation_id=self._id("evaluation"),
            phase=evaluation.phase,
            window_start=evaluation.window_start,
            window_end=evaluation.window_end,
            baseline=evaluation.baseline,
            candidate=evaluation.candidate,
            verdict=verdict,
            created_at=evaluation.created_at,
        )
        self.evaluations.setdefault(proposal_id, []).append(row)
        return row

    def workflow_pointer(self, workflow_id: str) -> P.WorkflowPointer | None:
        return P.WorkflowPointer(
            workflow_id=workflow_id,
            revision=self.revision,
            active_version_id="version-base",
            active_definition_hash=self.hash,
        )

    def active_canary(self, workflow_id: str) -> P.ProposalRecord | None:
        for record in self.proposals.values():
            if record.workflow_id == workflow_id and record.status is P.ProposalStatus.CANARY:
                return record
        return None

    def transition(
        self,
        proposal_id: str,
        *,
        expect_status: P.ProposalStatus,
        expect_revision: int,
        status: P.ProposalStatus,
        fields: Mapping[str, Any],
    ) -> P.ProposalRecord | None:
        record = self.proposals.get(proposal_id)
        if (
            record is None
            or record.status is not expect_status
            or record.workflow_revision != expect_revision
        ):
            return None
        updated = replace(record, status=status, updated_at=record.updated_at + 1, **fields)
        self.proposals[proposal_id] = updated
        return updated

    def apply_promotion(
        self, proposal_id: str, *, expect_revision: int, actor_user_id: int
    ) -> P.ProposalRecord | None:
        record = self.proposals.get(proposal_id)
        if record is None or record.status is not P.ProposalStatus.CANARY:
            return None
        if expect_revision != self.revision or record.workflow_revision != expect_revision:
            return None
        self.revision += 1
        self.hash = P.definition_hash({"applied": proposal_id})
        updated = replace(
            record,
            status=P.ProposalStatus.APPLIED,
            applied_version_id=self._id("promotion"),
            canary_stopped_at=record.updated_at + 1,
            canary_stop_reason="applied",
            updated_at=record.updated_at + 1,
        )
        self.proposals[proposal_id] = updated
        for sibling_id, sibling in list(self.proposals.items()):
            if (
                sibling_id != proposal_id
                and sibling.workflow_id == record.workflow_id
                and sibling.status in P.OPEN_STATUSES
            ):
                self.proposals[sibling_id] = replace(
                    sibling, status=P.ProposalStatus.SUPERSEDED, status_reason="superseded"
                )
        return updated


# --------------------------------------------------------------------------- #
# Service behaviour
# --------------------------------------------------------------------------- #

NOW = 1_700_000_000
MEDIUM_PATCH = [{"op": "replace", "path": "/nodes/0/config/prompt", "value": "be terse"}]
LOW_PATCH = [{"op": "replace", "path": "/nodes/0/name", "value": "Renamed"}]
ADMIN = P.ProposalActor(user_id=99, membership_id="m-99", is_admin=True)


def make_service(store: FakeStore) -> P.WorkBuddyProposalsService:
    return P.WorkBuddyProposalsService(store, policy=POLICY, now=lambda: NOW)


def create_proposal(
    service: P.WorkBuddyProposalsService,
    *,
    actor: P.ProposalActor,
    patch: Sequence[Mapping[str, Any]] = MEDIUM_PATCH,
    workflow_id: str = WORKFLOW_ID,
) -> P.ProposalRecord:
    return service.create(
        workflow_id=workflow_id,
        patch=patch,
        change_summary="tune",
        actor=actor,
        expect_revision=INITIAL_REVISION,
    ).proposal


def approve_twice(service: P.WorkBuddyProposalsService, proposal_id: str) -> None:
    service.decide(
        proposal_id, reviewer=P.ProposalActor(20), decision=P.ReviewDecision.APPROVED, comment=""
    )
    service.decide(
        proposal_id, reviewer=P.ProposalActor(21), decision=P.ReviewDecision.APPROVED, comment=""
    )


def settle_shadow(
    service: P.WorkBuddyProposalsService, proposal_id: str, *, runs: int = 10
) -> None:
    for index in range(runs):
        service.record_shadow_run(
            proposal_id,
            run=P.ShadowRunRow(
                run_id=f"shadow-{index}",
                settled=True,
                replay_only=True,
                live_side_effects=0,
                evidence_hash=f"hash-{index}",
                created_at=NOW,
            ),
        )


def start_canary(
    service: P.WorkBuddyProposalsService, proposal_id: str, *, ratio: int = 5000
) -> None:
    service.promote(
        proposal_id,
        action=P.PromotionAction.START_SHADOW,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
    )
    settle_shadow(service, proposal_id)
    service.promote(
        proposal_id,
        action=P.PromotionAction.START_CANARY,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
        ratio_basis_points=ratio,
    )


def record_canary_evaluation(
    service: P.WorkBuddyProposalsService,
    proposal_id: str,
    *,
    days: int = 8,
    candidate_runs: int = 300,
    safety_violations: int = 0,
) -> P.ProposalView:
    return service.record_evaluation(
        proposal_id,
        evaluation=P.NewEvaluation(
            phase="canary",
            window_start=NOW - days * P.CANARY_FULL_DAY_SECONDS,
            window_end=NOW,
            baseline=P.PhaseMetrics(500, 0.99, 1000.0, 1000.0),
            candidate=P.PhaseMetrics(candidate_runs, 0.985, 1050.0, 1010.0, safety_violations),
            created_at=NOW,
        ),
    )


def test_create_fixes_base_and_candidate_hashes() -> None:
    store = FakeStore(workflow_definition())
    service = make_service(store)
    record = create_proposal(service, actor=P.ProposalActor(10, "m-10"))

    assert record.base_content_hash == P.definition_hash(workflow_definition())
    assert record.candidate_content_hash != record.base_content_hash
    assert record.status is P.ProposalStatus.UNDER_REVIEW
    assert record.required_approvals == 2


def test_unknown_proposal_is_not_found() -> None:
    service = make_service(FakeStore(workflow_definition()))

    with pytest.raises(P.ProposalNotFoundError):
        service.get("does-not-exist")


def test_one_open_proposal_per_workflow() -> None:
    service = make_service(FakeStore(workflow_definition()))
    create_proposal(service, actor=P.ProposalActor(10))

    with pytest.raises(P.ProposalPolicyError) as caught:
        create_proposal(service, actor=P.ProposalActor(11))

    assert caught.value.code == "PROPOSAL_ALREADY_OPEN"


def test_creator_cannot_decide_own_proposal() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10, "m-10"))

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.decide(
            record.proposal_id,
            reviewer=P.ProposalActor(10, "m-10"),
            decision=P.ReviewDecision.APPROVED,
            comment="looks good",
        )

    assert caught.value.code == "CREATOR_SELF_REVIEW"


def test_reject_decision_is_terminal_and_beats_approvals() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(20),
        decision=P.ReviewDecision.APPROVED,
        comment="",
    )
    view = service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(21),
        decision=P.ReviewDecision.REJECTED,
        comment="risk",
    )

    assert view.proposal.status is P.ProposalStatus.REJECTED
    assert view.proposal.status_reason == "rejected"


def test_duplicate_review_is_refused_and_two_approvals_advance() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(20),
        decision=P.ReviewDecision.APPROVED,
        comment="",
    )

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.decide(
            record.proposal_id,
            reviewer=P.ProposalActor(20),
            decision=P.ReviewDecision.APPROVED,
            comment="",
        )
    assert caught.value.code == "DUPLICATE_REVIEW"

    view = service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(21),
        decision=P.ReviewDecision.APPROVED,
        comment="",
    )

    assert view.proposal.status is P.ProposalStatus.APPROVED
    assert [review.reviewer_user_id for review in view.reviews] == [20, 21]


def test_baseline_change_marks_the_proposal_stale_without_promoting() -> None:
    store = FakeStore(workflow_definition())
    service = make_service(store)
    record = create_proposal(service, actor=P.ProposalActor(10))
    store.revision += 1  # the workflow moved on

    view = service.get(record.proposal_id)

    assert view.proposal.status is P.ProposalStatus.STALE
    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.START_CANARY,
            if_match_revision=store.revision,
            actor=ADMIN,
            ratio_basis_points=1000,
        )
    assert caught.value.code == "PROPOSAL_STALE"


def test_promotion_requires_the_matching_workflow_revision() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.START_SHADOW,
            if_match_revision=INITIAL_REVISION - 1,
            actor=ADMIN,
        )

    assert caught.value.code == "STALE_REVISION"


def test_medium_risk_canary_requires_a_replay_only_shadow_proof() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, record.proposal_id)
    service.promote(
        record.proposal_id,
        action=P.PromotionAction.START_SHADOW,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
    )

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.START_CANARY,
            if_match_revision=INITIAL_REVISION,
            actor=ADMIN,
            ratio_basis_points=1000,
        )
    assert caught.value.code == "SHADOW_PROOF_REQUIRED"

    # A shadow runner that touched live systems is not a proof either.
    service.record_shadow_run(
        record.proposal_id,
        run=P.ShadowRunRow(
            "shadow-live", True, True, live_side_effects=1, evidence_hash="h", created_at=NOW
        ),
    )
    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.START_CANARY,
            if_match_revision=INITIAL_REVISION,
            actor=ADMIN,
            ratio_basis_points=1000,
        )
    assert caught.value.code == "SHADOW_PROOF_REQUIRED"
    assert caught.value.details["failures"] == [
        "insufficient_shadow_runs",
        "shadow_live_side_effects",
    ]


def test_low_risk_proposal_can_start_canary_without_shadow() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10), patch=LOW_PATCH)
    assert record.required_approvals == 1
    service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(20),
        decision=P.ReviewDecision.APPROVED,
        comment="",
    )

    view = service.promote(
        record.proposal_id,
        action=P.PromotionAction.START_CANARY,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
        ratio_basis_points=2500,
    )

    assert view.proposal.status is P.ProposalStatus.CANARY
    assert view.proposal.canary_ratio_bp == 2500


def test_insufficient_samples_never_promote() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, record.proposal_id)
    start_canary(service, record.proposal_id)
    record_canary_evaluation(service, record.proposal_id, days=3, candidate_runs=20)

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.APPLY,
            if_match_revision=INITIAL_REVISION,
            actor=ADMIN,
        )

    assert caught.value.code == "GATES_NOT_PASSED"
    assert set(caught.value.details["failures"]) >= {
        "insufficient_window",
        "insufficient_candidate_samples",
    }


def test_apply_before_any_canary_evaluation_is_refused() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, record.proposal_id)

    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            record.proposal_id,
            action=P.PromotionAction.APPLY,
            if_match_revision=INITIAL_REVISION,
            actor=ADMIN,
        )

    assert caught.value.code == "INVALID_STATE"


def test_successful_apply_is_serialized_by_revision_and_supersedes_siblings() -> None:
    store = FakeStore(workflow_definition())
    service = make_service(store)
    first = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, first.proposal_id)
    start_canary(service, first.proposal_id)
    record_canary_evaluation(service, first.proposal_id)

    # A second proposal drafted while the first runs is superseded when it lands.
    second = create_proposal(service, actor=P.ProposalActor(11))
    view = service.promote(
        first.proposal_id,
        action=P.PromotionAction.APPLY,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
    )

    assert view.proposal.status is P.ProposalStatus.APPLIED
    assert view.proposal.applied_version_id is not None
    assert view.proposal.canary_stop_reason == "applied"
    assert store.revision == INITIAL_REVISION + 1
    assert store.proposals[second.proposal_id].status is P.ProposalStatus.SUPERSEDED

    # A racing promotion that still holds the old revision loses the CAS.
    with pytest.raises(P.ProposalPolicyError) as caught:
        service.promote(
            first.proposal_id,
            action=P.PromotionAction.APPLY,
            if_match_revision=INITIAL_REVISION,
            actor=ADMIN,
        )
    assert caught.value.code in {"STALE_REVISION", "PROPOSAL_CHANGED", "INVALID_STATE"}


def test_safety_violation_stops_candidate_traffic() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, record.proposal_id)
    start_canary(service, record.proposal_id, ratio=9999)

    assert service.lane_for(WORKFLOW_ID, "any-key") == "candidate"

    view = record_canary_evaluation(service, record.proposal_id, safety_violations=1)

    assert view.proposal.status is P.ProposalStatus.ABORTED
    assert view.proposal.canary_stop_reason == "safety_violation"
    assert view.proposal.canary_stopped_at is not None
    assert service.lane_for(WORKFLOW_ID, "any-key") == "baseline"
    assert service.lane_for(WORKFLOW_ID, "another-key") == "baseline"


def test_reject_during_canary_stops_traffic() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    approve_twice(service, record.proposal_id)
    start_canary(service, record.proposal_id, ratio=9999)

    view = service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(31),
        decision=P.ReviewDecision.REJECTED,
        comment="stop",
    )

    assert view.proposal.status is P.ProposalStatus.REJECTED
    assert service.lane_for(WORKFLOW_ID, "any-key") == "baseline"


def test_abort_frees_the_workflow_for_a_new_proposal() -> None:
    service = make_service(FakeStore(workflow_definition()))
    record = create_proposal(service, actor=P.ProposalActor(10))
    service.decide(
        record.proposal_id,
        reviewer=P.ProposalActor(20),
        decision=P.ReviewDecision.APPROVED,
        comment="",
    )
    aborted = service.promote(
        record.proposal_id,
        action=P.PromotionAction.ABORT,
        if_match_revision=INITIAL_REVISION,
        actor=ADMIN,
    )

    assert aborted.proposal.status is P.ProposalStatus.ABORTED
    replacement = create_proposal(service, actor=P.ProposalActor(11))

    assert replacement.status is P.ProposalStatus.UNDER_REVIEW
