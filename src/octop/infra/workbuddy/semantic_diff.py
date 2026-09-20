"""The single keyed semantic diff shared by proposals and version comparison.

Workflow definitions are JSON documents whose node lists carry stable ``id``
values, so a positional walk would report a pure reorder as a rewrite of every
node.  :func:`semantic_diff` therefore matches identified objects by ``id`` and
compares mappings key by key; everything else compares element-wise.

Proposal policy and the version-comparison route both call this module, so a
change to ``versions/diff`` and a change to a proposal candidate are described
by exactly the same rules.  Everything here is deterministic and side-effect
free: no wall-clock time, no process or database state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["SemanticChange", "json_equal", "render_pointer", "semantic_diff"]


def json_equal(left: Any, right: Any) -> bool:
    """JSON equality that keeps ``bool`` distinct from numbers but not ``1``/``1.0``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return bool(left == right)


def render_pointer(tokens: Iterable[str]) -> str:
    """Encode tokens back into a JSON pointer."""
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


@dataclass(frozen=True, slots=True)
class SemanticChange:
    path: str
    kind: Literal["added", "removed", "replaced"]
    old: Any = None
    new: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "old": self.old, "new": self.new}


def _identity_key(item: Any) -> str | None:
    if isinstance(item, Mapping):
        for key in ("id", "node_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _keyed_list(items: Sequence[Any]) -> bool:
    keys = [_identity_key(item) for item in items]
    return bool(items) and all(key is not None for key in keys) and len(set(keys)) == len(keys)


def semantic_diff(
    base: Any, candidate: Any, *, path: tuple[str, ...] = ()
) -> tuple[SemanticChange, ...]:
    """Structural diff between two JSON documents.

    Mappings are compared by key; lists of identified objects (workflow nodes)
    are matched by ``id`` so reordering alone is not reported as a rewrite,
    while every real value change is.  Everything else compares element-wise.
    """
    changes: list[SemanticChange] = []
    if isinstance(base, Mapping) and isinstance(candidate, Mapping):
        for key in sorted(set(base) | set(candidate)):
            child = (*path, key)
            if key not in base:
                changes.append(SemanticChange(render_pointer(child), "added", None, candidate[key]))
            elif key not in candidate:
                changes.append(SemanticChange(render_pointer(child), "removed", base[key], None))
            else:
                changes.extend(semantic_diff(base[key], candidate[key], path=child))
        return tuple(changes)
    if isinstance(base, list) and isinstance(candidate, list):
        if _keyed_list(base) and _keyed_list(candidate):
            base_by_id = {_identity_key(item): item for item in base}
            candidate_by_id = {_identity_key(item): item for item in candidate}
            for node_id in base_by_id:
                child = (*path, node_id)
                if node_id not in candidate_by_id:
                    changes.append(
                        SemanticChange(render_pointer(child), "removed", base_by_id[node_id], None)
                    )
                else:
                    changes.extend(
                        semantic_diff(base_by_id[node_id], candidate_by_id[node_id], path=child)
                    )
            for node_id in candidate_by_id:
                if node_id not in base_by_id:
                    child = (*path, node_id)
                    changes.append(
                        SemanticChange(
                            render_pointer(child), "added", None, candidate_by_id[node_id]
                        )
                    )
            return tuple(changes)
        for index in range(max(len(base), len(candidate))):
            child = (*path, str(index))
            if index >= len(base):
                changes.append(
                    SemanticChange(render_pointer(child), "added", None, candidate[index])
                )
            elif index >= len(candidate):
                changes.append(SemanticChange(render_pointer(child), "removed", base[index], None))
            else:
                changes.extend(semantic_diff(base[index], candidate[index], path=child))
        return tuple(changes)
    if not json_equal(base, candidate):
        changes.append(SemanticChange(render_pointer(path) or "/", "replaced", base, candidate))
    return tuple(changes)
