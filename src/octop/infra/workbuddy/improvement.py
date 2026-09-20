"""Turning corrections into something the governance chain can review (A-14).

A-13 says *where* a correction belongs.  This module takes that one step further:
it asks for a concrete change, in the chain's own language (a JSON Patch), and
hands it to the existing proposal service to compile, grade and route.

Three rules keep this from becoming a machine that rewrites workflows on its own:

* **Evidence threshold.**  A single correction is one bad day; the same value
  corrected twice is a pattern.  Clusters below ``minimum_corrections`` are
  reported as skipped, not sent anywhere.
* **The chain decides.**  Every suggestion is created through
  ``WorkBuddyProposalsService``, so it is compiled, schema-checked, policy-checked
  and risk-graded exactly like a person's proposal.  A suggestion that fails any
  of that is recorded as rejected; nothing is written that the chain refused.
* **Scoped targets.**  A suggestion may only touch a step that was actually
  analysed.  Anything else is a model answering a different question.

The suggestion source is a port, like every other external dependency here: when a
deployment has not wired one, asking fails closed rather than inventing a change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.attribution import CorrectionCluster
from octop.infra.workbuddy.proposals import ProposalActor, ProposalPolicyError

#: Corrections needed before a cluster is worth acting on.  Two, because one
#: corrected value says the model was wrong once and twice says it is wrong here.
MIN_CORRECTIONS_FOR_SUGGESTION = 2


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One proposed change, addressed at the step it belongs to."""

    node_id: str
    output_key: str
    rationale: str
    patch: tuple[Mapping[str, Any], ...]


class SuggestionSource(Protocol):
    """Whoever turns evidence into a proposal: a model adapter, or a test double."""

    def suggest(
        self,
        *,
        definition: Mapping[str, Any],
        clusters: Sequence[CorrectionCluster],
    ) -> Sequence[Suggestion]: ...


class ProposalCreator(Protocol):
    """The slice of the proposal service this module uses."""

    def create(
        self,
        *,
        workflow_id: str,
        patch: Sequence[Mapping[str, Any]],
        change_summary: str,
        actor: ProposalActor,
        expect_revision: int,
    ) -> Any: ...


class UnavailableSuggestionSource:
    """Fails closed when no analyser is wired into this deployment."""

    def suggest(
        self,
        *,
        definition: Mapping[str, Any],
        clusters: Sequence[CorrectionCluster],
    ) -> Sequence[Suggestion]:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "no improvement analyser is configured",
        )


UNAVAILABLE_SUGGESTIONS = UnavailableSuggestionSource()


@dataclass(frozen=True, slots=True)
class ImprovementOutcome:
    """What the analyser did: created proposals, refused ones, and untried evidence."""

    created: tuple[Any, ...]
    rejected: tuple[Mapping[str, Any], ...]
    skipped: tuple[Mapping[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "created": [
                {
                    "proposal_id": view.proposal.proposal_id,
                    "workflow_id": view.proposal.workflow_id,
                    "status": str(view.proposal.status),
                    "risk_level": view.proposal.risk_level,
                    "required_approvals": view.proposal.required_approvals,
                    "change_summary": view.proposal.change_summary,
                }
                for view in self.created
            ],
            "rejected": [dict(item) for item in self.rejected],
            "skipped": [dict(item) for item in self.skipped],
        }


def eligible_clusters(
    clusters: Sequence[CorrectionCluster],
    *,
    minimum_corrections: int = MIN_CORRECTIONS_FOR_SUGGESTION,
) -> list[CorrectionCluster]:
    """Clusters with enough evidence to justify asking for a change."""
    return [
        cluster
        for cluster in clusters
        if cluster.kind == "correction" and cluster.corrections >= minimum_corrections
    ]


def analyse_and_propose(
    *,
    workflow_id: str,
    definition: Mapping[str, Any],
    clusters: Sequence[CorrectionCluster],
    source: SuggestionSource,
    proposals: ProposalCreator,
    actor: ProposalActor,
    expect_revision: int,
    minimum_corrections: int = MIN_CORRECTIONS_FOR_SUGGESTION,
) -> ImprovementOutcome:
    """Ask for changes on the clusters that earned it, and route them as proposals.

    The analyser never writes a workflow.  It creates proposals, and the chain's own
    compile/policy/grading decides what becomes of them — so the worst case of a
    wrong suggestion is a draft somebody declines to review.
    """
    eligible = eligible_clusters(clusters, minimum_corrections=minimum_corrections)
    skipped = tuple(
        {
            "node_id": cluster.node_id,
            "output_key": cluster.output_key,
            "kind": cluster.kind,
            "corrections": cluster.corrections,
            "reason": (
                "not_a_correction"
                if cluster.kind != "correction"
                else "insufficient_evidence"
            ),
        }
        for cluster in clusters
        if cluster not in eligible
    )
    if not eligible:
        # Nothing earned a change, so nothing is asked of the source: an analyser
        # that calls a model to be told "no" is paying for its own noise.
        return ImprovementOutcome(created=(), rejected=(), skipped=skipped)

    allowed = {cluster.node_id for cluster in eligible}
    created: list[Any] = []
    rejected: list[Mapping[str, Any]] = []
    for suggestion in source.suggest(definition=definition, clusters=eligible):
        patch = tuple(suggestion.patch)
        if suggestion.node_id not in allowed:
            rejected.append(
                {
                    "node_id": suggestion.node_id,
                    "code": "SUGGESTION_TARGET_UNKNOWN",
                    "message": "the suggested change is not about a step that was analysed",
                }
            )
            continue
        if not patch:
            rejected.append(
                {
                    "node_id": suggestion.node_id,
                    "code": "SUGGESTION_EMPTY",
                    "message": "the suggested change carries no patch",
                }
            )
            continue
        try:
            view = proposals.create(
                workflow_id=workflow_id,
                patch=patch,
                change_summary=suggestion.rationale,
                actor=actor,
                expect_revision=expect_revision,
            )
        except ProposalPolicyError as exc:
            # The chain refused it (does not compile, breaks policy, stale revision).
            # That refusal stands; this module records it instead of working around it.
            rejected.append(
                {"node_id": suggestion.node_id, "code": exc.code, "message": exc.message}
            )
            continue
        created.append(view)
    return ImprovementOutcome(created=tuple(created), rejected=tuple(rejected), skipped=skipped)


__all__ = [
    "MIN_CORRECTIONS_FOR_SUGGESTION",
    "UNAVAILABLE_SUGGESTIONS",
    "ImprovementOutcome",
    "ProposalCreator",
    "Suggestion",
    "SuggestionSource",
    "UnavailableSuggestionSource",
    "analyse_and_propose",
    "eligible_clusters",
]
