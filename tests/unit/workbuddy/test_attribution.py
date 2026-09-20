"""Scope of a correction: which step produced it, and what produced *it* (A-13).

The point of this module is to turn "a person changed this value, twice, in two
runs" into something triageable.  A correction names a value the run produced, so
the only place a fix could go is upstream — the steps that contributed to it.  A
supplied fact is the opposite: it was missing, so what matters is what its answer
unblocked.  These tests pin that asymmetry, the grouping, and the cases where
reporting nothing is the honest answer.
"""

from __future__ import annotations

from types import SimpleNamespace

from octop.infra.workbuddy.attribution import (
    CORRECTION_KIND,
    MAX_EXAMPLES_PER_CLUSTER,
    SUPPLIED_FACT_KIND,
    attribute_corrections,
)
from octop.infra.workbuddy.workflow_compiler import compile_workflow_definition


def _compiled():
    """start → price → summary, plus start → audit: one value with two readers."""
    definition = {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"seed": {"type": "string", "required": True}},
        "nodes": [
            {
                "id": "start",
                "type": "transform",
                "name": "Start",
                "config": {"input": {"x": "{{ inputs.seed }}"}, "expression": "input"},
                "save_as": "started",
            },
            {
                "id": "price",
                "type": "transform",
                "name": "Price",
                "config": {"input": "{{ nodes.start.output }}", "expression": "input"},
                "save_as": "price",
            },
            {
                "id": "summary",
                "type": "transform",
                "name": "Summary",
                "config": {"input": "{{ nodes.price.output }}", "expression": "input"},
                "save_as": "summary",
            },
            {
                "id": "audit",
                "type": "transform",
                "name": "Audit",
                "config": {"input": "{{ nodes.start.output }}", "expression": "input"},
                "save_as": "audit",
            },
        ],
        "edges": [
            {"from": "start", "to": "price"},
            {"from": "price", "to": "summary"},
            {"from": "start", "to": "audit"},
        ],
    }
    return compile_workflow_definition(definition)


def _row(**overrides: object) -> SimpleNamespace:
    base = {
        "execution_id": "run-1",
        "node_id": None,
        "output_key": "price",
        "kind": CORRECTION_KIND,
        "before": 100,
        "after": 120,
        "created_at": "2026-09-20T00:00:00+00:00",
    }
    return SimpleNamespace(**{**base, **overrides})


def test_a_correction_points_upstream_at_the_steps_that_made_it() -> None:
    compiled = _compiled()
    clusters = attribute_corrections(
        compiled,
        [
            _row(execution_id="run-1", before=100, after=120),
            _row(execution_id="run-2", before=110, after=130),
        ],
    )
    assert len(clusters) == 1, clusters
    cluster = clusters[0]
    assert cluster.node_id == "price" and cluster.output_key == "price", cluster
    assert cluster.corrections == 2 and cluster.executions == 2, cluster
    assert cluster.direction == "upstream", cluster
    # Only ``start`` contributed; ``audit`` is a sibling that reads the same value
    # and has nothing to do with how ``price`` came out.
    assert cluster.scope_node_ids == ("start",), cluster
    assert cluster.scope_output_keys == ("started",), cluster


def test_a_value_its_step_produced_alone_reports_an_empty_upstream() -> None:
    compiled = _compiled()
    clusters = attribute_corrections(compiled, [_row(output_key="started")])
    assert len(clusters) == 1, clusters
    # ``started`` comes from the run's first step, so there is no chain to trace:
    # that step itself is the place to look, and the empty tuple says exactly that.
    assert clusters[0].scope_node_ids == (), clusters[0]
    assert clusters[0].scope_output_keys == (), clusters[0]


def test_corrections_and_supplied_facts_are_separate_clusters() -> None:
    compiled = _compiled()
    clusters = attribute_corrections(
        compiled,
        [
            _row(output_key="price"),
            _row(
                kind=SUPPLIED_FACT_KIND,
                node_id="price",
                output_key=None,
                before=None,
                after={"quote": "120"},
            ),
        ],
    )
    kinds = sorted(cluster.kind for cluster in clusters)
    assert kinds == [CORRECTION_KIND, SUPPLIED_FACT_KIND], clusters
    fact = next(cluster for cluster in clusters if cluster.kind == SUPPLIED_FACT_KIND)
    assert fact.node_id == "price" and fact.output_key == "price", fact
    # A missing fact points the other way: what its answer unblocked.
    assert fact.direction == "downstream", fact
    assert fact.scope_node_ids == ("summary",), fact
    assert fact.examples[0]["before"] is None, fact


def test_a_key_the_version_no_longer_produces_is_left_out() -> None:
    compiled = _compiled()
    # The workflow was republished and the key is gone: attributing it to some
    # other step would be a guess, so it is dropped instead.
    assert attribute_corrections(compiled, [_row(output_key="vanished")]) == []


def test_the_most_corrected_step_comes_first_and_examples_are_bounded() -> None:
    compiled = _compiled()
    rows = [_row(output_key="price", after=index) for index in range(5)]
    rows.append(_row(output_key="audit"))
    clusters = attribute_corrections(compiled, rows)
    assert [cluster.output_key for cluster in clusters] == ["price", "audit"], clusters
    assert clusters[0].corrections == 5, clusters[0]
    assert len(clusters[0].examples) == MAX_EXAMPLES_PER_CLUSTER, clusters[0]
    # The kept examples are the newest ones, with the before/after pair intact.
    assert [example["after"] for example in clusters[0].examples] == [2, 3, 4], clusters[0]
