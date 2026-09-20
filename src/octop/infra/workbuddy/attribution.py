"""What a correction means for the rest of the workflow (A-13).

A correction on its own says one value was wrong once.  What the improvement loop
needs next is *scope*: which step produced it, and what else that step's output
feeds — because a value nobody downstream reads is a display fix, while one that
feeds an external write is a different conversation entirely.

Two deliberate limits, both to keep this honest rather than clever:

* Scope is computed on the **compiled graph** of the version the run executed, by
  walking edges.  It never guesses business semantics: "upstream" means "these
  nodes contributed to the value", nothing more.
* A cluster counts and shows evidence; it does not decide what to change.  That
  judgement belongs to a person reading the cluster, and later to a proposal (A-14).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from octop.infra.workbuddy.workflow_compiler import CompiledWorkflow

#: How many before/after pairs a cluster keeps as evidence.  Enough to see the
#: pattern, bounded so one noisy node cannot blow up the payload.
MAX_EXAMPLES_PER_CLUSTER = 3

#: Kinds of feedback this module attributes.  A supplied fact is not a correction,
#: but it points at the same question — "this step's input was not available" — so
#: it is reported as its own cluster kind instead of being mixed in.
CORRECTION_KIND = "correction"
SUPPLIED_FACT_KIND = "supplied_fact"


@dataclass(frozen=True, slots=True)
class CorrectionCluster:
    """One (node, output key) a person kept changing, with the scope it points at.

    The scope direction differs by kind, and that is the whole point:

    * a **correction** names a value the run *produced*, so the useful direction is
      upward — the steps that contributed to it are the only places a fix could go
      (``direction="upstream"``);
    * a **supplied fact** was missing until somebody answered, so the useful
      direction is downward — what that answer unblocked (``direction="downstream"``).

    A correction whose upstream is empty was produced by its own step alone: that is
    a step to look at, not a chain to trace.
    """

    node_id: str
    node_type: str
    output_key: str
    kind: str
    direction: str
    corrections: int
    executions: int
    first_seen: Any
    last_seen: Any
    scope_node_ids: tuple[str, ...]
    scope_output_keys: tuple[str, ...]
    examples: tuple[Mapping[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "output_key": self.output_key,
            "kind": self.kind,
            "corrections": self.corrections,
            "executions": self.executions,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "direction": self.direction,
            "scope_node_ids": list(self.scope_node_ids),
            "scope_output_keys": list(self.scope_output_keys),
            "examples": [dict(example) for example in self.examples],
        }


def _scope(
    compiled: CompiledWorkflow, node_id: str, *, direction: str
) -> tuple[list[str], list[str]]:
    """The node closure in one direction, and the output keys those nodes carry.

    ``downstream`` walks outgoing edges (what consumes this step's value),
    ``upstream`` walks incoming ones (what contributed to it).  A definition may fan
    out and join, so this is a closure, not a path: everything reachable is in scope.
    """
    seen: set[str] = set()
    pending = [node_id]
    while pending:
        current = pending.pop()
        node = compiled.node_by_id.get(current)
        if node is None:
            continue
        edges = node.outgoing if direction == "downstream" else node.incoming
        for edge in edges:
            neighbour = edge.to_node_id if direction == "downstream" else edge.from_node_id
            if neighbour in seen:
                continue
            seen.add(neighbour)
            pending.append(neighbour)
    seen.discard(node_id)
    nodes = sorted(seen)
    keys = sorted(
        {
            compiled.output_key_by_node[reached]
            for reached in nodes
            if reached in compiled.output_key_by_node
        }
    )
    return nodes, keys


def attribute_corrections(
    compiled: CompiledWorkflow,
    rows: Iterable[Any],
    *,
    output_key_to_node: Mapping[str, str] | None = None,
) -> list[CorrectionCluster]:
    """Group feedback rows into clusters, each with the downstream scope it affects.

    Rows are grouped by the node and output key they concern.  A row whose output
    key no longer resolves to a node (the version was republished and the key is
    gone) is skipped rather than guessed at: attributing it to the wrong step would
    be worse than leaving it out, and the caller can still see the raw rows.
    """
    key_to_node = dict(output_key_to_node or compiled.output_key_to_node)
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        node_id = str(getattr(row, "node_id", "") or "")
        output_key = str(getattr(row, "output_key", "") or "")
        kind = str(getattr(row, "kind", "") or "")
        if kind == CORRECTION_KIND:
            if not output_key:
                continue
            node_id = key_to_node.get(output_key, node_id)
        elif kind == SUPPLIED_FACT_KIND:
            if not node_id:
                continue
            output_key = output_key or node_id
        else:
            continue
        if node_id not in compiled.node_by_id:
            continue
        grouped[(node_id, output_key, kind)].append(row)

    clusters: list[CorrectionCluster] = []
    for (node_id, output_key, kind), members in grouped.items():
        ordered = sorted(members, key=lambda row: (str(getattr(row, "created_at", "")),))
        executions = {str(getattr(row, "execution_id", "")) for row in ordered}
        node = compiled.node_by_id[node_id]
        # A produced value points upward (what made it), a missing fact downward
        # (what it unblocked). Computing the other direction is the difference
        # between "steps to look at" and "steps that were waiting".
        direction = "upstream" if kind == CORRECTION_KIND else "downstream"
        scope_nodes, scope_keys = _scope(compiled, node_id, direction=direction)
        clusters.append(
            CorrectionCluster(
                node_id=node_id,
                node_type=node.node_type,
                output_key=output_key,
                kind=kind,
                direction=direction,
                corrections=len(ordered),
                executions=len(executions),
                first_seen=_seen_at(ordered[0]),
                last_seen=_seen_at(ordered[-1]),
                scope_node_ids=tuple(scope_nodes),
                scope_output_keys=tuple(scope_keys),
                examples=tuple(
                    {
                        "execution_id": str(getattr(row, "execution_id", "")),
                        "before": getattr(row, "before", None),
                        "after": getattr(row, "after", None),
                    }
                    for row in ordered[-MAX_EXAMPLES_PER_CLUSTER:]
                ),
            )
        )
    # Most-corrected first: that is the order a person triaging this wants.
    clusters.sort(key=lambda cluster: (-cluster.corrections, cluster.node_id, cluster.output_key))
    return clusters


def _seen_at(row: Any) -> Any:
    value = getattr(row, "created_at", None)
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value


__all__ = [
    "CORRECTION_KIND",
    "MAX_EXAMPLES_PER_CLUSTER",
    "SUPPLIED_FACT_KIND",
    "CorrectionCluster",
    "attribute_corrections",
]
