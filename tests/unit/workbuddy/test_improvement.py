"""The analyser asks for changes only when the evidence earns it (A-14).

This is the one place in the improvement loop where a machine proposes an edit to
somebody's workflow, so the tests are mostly about restraint: when it does *not*
call the analyser, when it refuses a suggestion, and how it behaves when the
governance chain rejects one.  The rule being protected throughout: the analyser
never writes a workflow — the proposal chain decides what happens to anything it
suggests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from octop.infra.workbuddy.attribution import CorrectionCluster
from octop.infra.workbuddy.improvement import (
    ImprovementOutcome,
    Suggestion,
    analyse_and_propose,
)
from octop.infra.workbuddy.proposals import ProposalActor, ProposalPolicyError

DEFINITION: Mapping[str, Any] = {"schema_version": 1, "nodes": [{"id": "polish"}]}


def _cluster(**overrides: Any) -> CorrectionCluster:
    base: dict[str, Any] = {
        "node_id": "polish",
        "node_type": "transform",
        "output_key": "polished",
        "kind": "correction",
        "direction": "upstream",
        "corrections": 2,
        "executions": 2,
        "first_seen": "2026-09-20T00:00:00+00:00",
        "last_seen": "2026-09-20T01:00:00+00:00",
        "scope_node_ids": ("greet",),
        "scope_output_keys": ("greeting",),
        "examples": (),
    }
    return CorrectionCluster(**{**base, **overrides})


@dataclass
class RecordingSource:
    """A suggestion source that remembers whether it was consulted at all."""

    suggestions: Sequence[Suggestion] = ()
    calls: list[Sequence[CorrectionCluster]] = field(default_factory=list)

    def suggest(
        self, *, definition: Mapping[str, Any], clusters: Sequence[CorrectionCluster]
    ) -> Sequence[Suggestion]:
        self.calls.append(clusters)
        return self.suggestions


@dataclass
class StubProposals:
    """Stands in for the proposal service, recording what it was asked to create.

    ``refusals`` are handed out one per call and then exhausted, so a test can say
    "the chain refuses the first suggestion and accepts the second" — which is the
    situation the analyser has to survive without losing the good suggestion.
    """

    refusals: list[tuple[str, str]] = field(default_factory=list)
    requests: list[Mapping[str, Any]] = field(default_factory=list)

    def create(
        self,
        *,
        workflow_id: str,
        patch: Sequence[Mapping[str, Any]],
        change_summary: str,
        actor: ProposalActor,
        expect_revision: int,
    ) -> Any:
        self.requests.append(
            {
                "workflow_id": workflow_id,
                "patch": tuple(patch),
                "change_summary": change_summary,
                "expect_revision": expect_revision,
            }
        )
        if self.refusals:
            raise ProposalPolicyError(*self.refusals.pop(0))
        return _View(_Proposal(workflow_id=workflow_id, change_summary=change_summary))


@dataclass(frozen=True)
class _Proposal:
    proposal_id: str = "prop-1"
    workflow_id: str = "wf-1"
    status: str = "draft"
    risk_level: str = "low"
    required_approvals: int = 1
    change_summary: str = "改一下"


@dataclass(frozen=True)
class _View:
    proposal: _Proposal = _Proposal()


ACTOR = ProposalActor(user_id=7)


def _patch() -> tuple[Mapping[str, Any], ...]:
    return ({"op": "replace", "path": "/nodes/0/name", "value": "Polish v2"},)


def test_two_corrections_are_enough_to_ask_for_a_change() -> None:
    source = RecordingSource([Suggestion("polish", "polished", "两次都改这里", _patch())])
    proposals = StubProposals()
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster()],
        source=source,
        proposals=proposals,
        actor=ACTOR,
        expect_revision=3,
    )
    assert isinstance(outcome, ImprovementOutcome)
    assert len(outcome.created) == 1 and outcome.rejected == (), outcome
    assert source.calls and len(source.calls[0]) == 1, source.calls
    request = proposals.requests[0]
    assert request["patch"] == _patch(), request
    assert request["change_summary"] == "两次都改这里", request
    # The suggestion is a proposal against the revision the analysis was made on.
    assert request["expect_revision"] == 3, request


def test_one_correction_is_not_a_pattern_so_the_analyser_is_not_consulted() -> None:
    source = RecordingSource([Suggestion("polish", "polished", "改", _patch())])
    proposals = StubProposals()
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster(corrections=1, executions=1)],
        source=source,
        proposals=proposals,
        actor=ACTOR,
        expect_revision=1,
    )
    assert outcome.created == () and outcome.rejected == (), outcome
    assert [item["reason"] for item in outcome.skipped] == ["insufficient_evidence"], outcome
    # Paying a model to be told "no" is the failure mode being prevented here.
    assert source.calls == [], source.calls
    assert proposals.requests == [], proposals.requests


def test_a_supplied_fact_is_evidence_for_a_person_not_a_change_request() -> None:
    source = RecordingSource()
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster(kind="supplied_fact", corrections=5)],
        source=source,
        proposals=StubProposals(),
        actor=ACTOR,
        expect_revision=1,
    )
    assert [item["reason"] for item in outcome.skipped] == ["not_a_correction"], outcome
    assert source.calls == [], source.calls


def test_a_suggestion_about_an_unanalysed_step_is_refused() -> None:
    source = RecordingSource([Suggestion("somewhere-else", "x", "顺手改改", _patch())])
    proposals = StubProposals()
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster()],
        source=source,
        proposals=proposals,
        actor=ACTOR,
        expect_revision=1,
    )
    assert outcome.created == (), outcome
    assert [item["code"] for item in outcome.rejected] == ["SUGGESTION_TARGET_UNKNOWN"], outcome
    assert proposals.requests == [], proposals.requests


def test_a_change_the_chain_refuses_is_recorded_and_not_worked_around() -> None:
    source = RecordingSource(
        [
            Suggestion("polish", "polished", "改坏了", ({"op": "remove", "path": "/nodes"},)),
            Suggestion("polish", "polished", "第二次尝试", _patch()),
        ]
    )
    # The chain refuses the first and accepts the second: one bad suggestion must
    # not cost the good ones, and the refusal must survive in the report.
    proposals = StubProposals(refusals=[("WORKBUDDY_PATCH_INVALID", "/nodes 不能被删除")])
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster()],
        source=source,
        proposals=proposals,
        actor=ACTOR,
        expect_revision=1,
    )
    assert [item["code"] for item in outcome.rejected] == ["WORKBUDDY_PATCH_INVALID"], outcome
    assert len(proposals.requests) == 2, proposals.requests
    payload = outcome.to_payload()
    # The refused one is reported, the accepted one still became a proposal.
    assert len(payload["created"]) == 1, payload
    assert payload["created"][0]["change_summary"] == "第二次尝试", payload
    assert payload["rejected"][0]["message"] == "/nodes 不能被删除", payload


def test_a_suggestion_without_a_patch_is_refused() -> None:
    source = RecordingSource([Suggestion("polish", "polished", "空话", ())])
    proposals = StubProposals()
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster()],
        source=source,
        proposals=proposals,
        actor=ACTOR,
        expect_revision=1,
    )
    assert [item["code"] for item in outcome.rejected] == ["SUGGESTION_EMPTY"], outcome
    assert proposals.requests == [], proposals.requests


def test_the_outcome_payload_names_the_created_proposals() -> None:
    source = RecordingSource([Suggestion("polish", "polished", "两次都改这里", _patch())])
    outcome = analyse_and_propose(
        workflow_id="wf-1",
        definition=DEFINITION,
        clusters=[_cluster()],
        source=source,
        proposals=StubProposals(),
        actor=ACTOR,
        expect_revision=1,
    )
    payload = outcome.to_payload()
    assert payload["created"][0]["proposal_id"] == "prop-1", payload
    assert payload["created"][0]["change_summary"] == "两次都改这里", payload
    assert payload["skipped"] == [], payload


def test_no_analyser_wired_fails_closed() -> None:
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.workbuddy.improvement import UNAVAILABLE_SUGGESTIONS

    with pytest.raises(OctopError) as raised:
        UNAVAILABLE_SUGGESTIONS.suggest(definition=DEFINITION, clusters=[_cluster()])
    assert raised.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE, raised.value
