"""WorkBuddy improvement proposals: patching, semantics, gates, promotion.

Everything in this module is deterministic and side-effect free.  A proposal is
compiled from an immutable base definition plus an RFC 6902 patch; the compiled
candidate hash, semantic diff, risk assessment, approval requirement and canary
bucket therefore never depend on wall-clock time or process state.

Two invariants drive the design:

* The *final semantic diff* is the only source of truth for policy.  Patch
  operations are never trusted to describe their own effect, so path tricks
  (``~1`` escapes, whole-document replacements, reordering) cannot hide a change
  from the boundary checks.
* Nothing that cannot be proven promotes: gates and shadow proofs must be
  complete, replay-only and settled, otherwise the candidate stays where it is.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "CANARY_BUCKET_MODULUS",
    "CANARY_MIN_FULL_DAYS",
    "CANARY_MIN_SETTLED_RUNS",
    "MAX_PATCH_OPERATIONS",
    "OPEN_STATUSES",
    "PENDING_STATUSES",
    "SHADOW_MIN_SETTLED_RUNS",
    "ApprovalOutcome",
    "ApprovalState",
    "CanaryEvidence",
    "CompiledProposal",
    "EvaluationRow",
    "GateThresholds",
    "GateVerdict",
    "NewEvaluation",
    "NewProposal",
    "NewReview",
    "PatchError",
    "PatchOperation",
    "PhaseMetrics",
    "PolicyViolation",
    "ProposalActor",
    "ProposalConflictError",
    "ProposalNotFoundError",
    "ProposalPolicy",
    "ProposalPolicyError",
    "ProposalRecord",
    "ProposalStatus",
    "ProposalStore",
    "ProposalView",
    "PromotionAction",
    "ReviewDecision",
    "ReviewRecord",
    "ReviewVote",
    "RiskAssessment",
    "SemanticChange",
    "ShadowProof",
    "ShadowRunRecord",
    "ShadowRunRow",
    "WorkBuddyProposalsService",
    "WorkflowPointer",
    "apply_patch",
    "canary_bucket",
    "canary_key",
    "canary_lane",
    "classification_for",
    "compile_proposal",
    "definition_hash",
    "evaluate_approvals",
    "evaluate_canary_gates",
    "evaluate_shadow_proof",
    "is_canary_selected",
    "parse_patch",
    "semantic_diff",
    "transition_for",
    "validate_definition",
]

CANARY_BUCKET_MODULUS = 10_000
CANARY_MIN_FULL_DAYS = 7
CANARY_MIN_SETTLED_RUNS = 100
SHADOW_MIN_SETTLED_RUNS = 10
MAX_PATCH_OPERATIONS = 200
CANARY_FULL_DAY_SECONDS = 86_400
ALLOWLIST_UNAVAILABLE = "TOOL_ALLOWLIST_UNAVAILABLE"

_WORKFLOW_SCHEMA_RELPATH = Path("contracts") / "workflow-v1.schema.json"


class ProposalPolicyError(ValueError):
    """A deterministic policy refusal; ``code`` is stable for clients."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class PatchError(ValueError):
    """Malformed or inapplicable RFC 6902 patch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Canonical JSON and hashing
# --------------------------------------------------------------------------- #


def canonical_json(value: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 escapes preserved."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def definition_hash(definition: Any) -> str:
    """SHA-256 over the canonical UTF-8 encoding of a workflow definition."""
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality that keeps ``bool`` distinct from numbers but not ``1``/``1.0``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return bool(left == right)


# --------------------------------------------------------------------------- #
# JSON pointers (RFC 6901)
# --------------------------------------------------------------------------- #

_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_INVALID_ESCAPE_RE = re.compile(r"~(?![01])")


def parse_pointer(pointer: str) -> tuple[str, ...]:
    """Split an RFC 6901 JSON pointer into decoded tokens."""
    if pointer == "":
        return ()
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PatchError("invalid_pointer", f"pointer must be empty or start with '/': {pointer!r}")
    tokens: list[str] = []
    for raw in pointer.split("/")[1:]:
        if _INVALID_ESCAPE_RE.search(raw):
            raise PatchError("invalid_pointer", f"invalid escape sequence in pointer: {pointer!r}")
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


def render_pointer(tokens: Iterable[str]) -> str:
    """Encode tokens back into a JSON pointer."""
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _array_index(token: str, length: int, *, allow_append: bool) -> int:
    if token == "-":
        if allow_append:
            return length
        raise PatchError("invalid_index", "'-' is only valid when adding to an array")
    if not _INDEX_RE.match(token):
        raise PatchError("invalid_index", f"invalid array index {token!r}")
    index = int(token)
    limit = length if allow_append else length - 1
    if index > limit:
        raise PatchError("invalid_index", f"array index {index} out of range (length {length})")
    return index


def _resolve_parent(document: Any, tokens: Sequence[str]) -> tuple[Any, str]:
    if not tokens:
        raise PatchError("invalid_pointer", "operation cannot target the document root")
    node = document
    for token in tokens[:-1]:
        if isinstance(node, Mapping):
            if token not in node:
                raise PatchError("missing_target", f"path segment {token!r} does not exist")
            node = node[token]
        elif isinstance(node, list):
            node = node[_array_index(token, len(node), allow_append=False)]
        else:
            raise PatchError("missing_target", f"cannot descend into scalar at {token!r}")
    return node, tokens[-1]


def _pointer_get(document: Any, tokens: Sequence[str]) -> Any:
    node = document
    for token in tokens:
        if isinstance(node, Mapping):
            if token not in node:
                raise PatchError("missing_target", f"path segment {token!r} does not exist")
            node = node[token]
        elif isinstance(node, list):
            node = node[_array_index(token, len(node), allow_append=False)]
        else:
            raise PatchError("missing_target", f"cannot descend into scalar at {token!r}")
    return node


def _pointer_add(document: Any, tokens: Sequence[str], value: Any) -> None:
    parent, token = _resolve_parent(document, tokens)
    if isinstance(parent, MutableMapping):
        parent[token] = value
    elif isinstance(parent, list):
        parent.insert(_array_index(token, len(parent), allow_append=True), value)
    else:
        raise PatchError("missing_target", f"cannot add into scalar at {token!r}")


def _pointer_remove(document: Any, tokens: Sequence[str]) -> Any:
    parent, token = _resolve_parent(document, tokens)
    if isinstance(parent, MutableMapping):
        if token not in parent:
            raise PatchError("missing_target", f"path segment {token!r} does not exist")
        return parent.pop(token)
    if isinstance(parent, list):
        return parent.pop(_array_index(token, len(parent), allow_append=False))
    raise PatchError("missing_target", f"cannot remove from scalar at {token!r}")


def _pointer_replace(document: Any, tokens: Sequence[str], value: Any) -> None:
    if not tokens:
        raise PatchError("invalid_pointer", "operation cannot target the document root")
    parent, token = _resolve_parent(document, tokens)
    if isinstance(parent, MutableMapping):
        if token not in parent:
            raise PatchError("missing_target", f"path segment {token!r} does not exist")
        parent[token] = value
        return
    if isinstance(parent, list):
        parent[_array_index(token, len(parent), allow_append=False)] = value
        return
    raise PatchError("missing_target", f"cannot replace in scalar at {token!r}")


# --------------------------------------------------------------------------- #
# RFC 6902 application
# --------------------------------------------------------------------------- #

_ADD, _REMOVE, _REPLACE, _MOVE, _COPY, _TEST = "add", "remove", "replace", "move", "copy", "test"
_PATCH_OPS = frozenset({_ADD, _REMOVE, _REPLACE, _MOVE, _COPY, _TEST})
_VALUE_OPS = frozenset({_ADD, _REPLACE, _TEST})
_FROM_OPS = frozenset({_MOVE, _COPY})

_MISSING = object()


@dataclass(frozen=True, slots=True)
class PatchOperation:
    op: str
    path: str
    value: Any = _MISSING
    from_path: str | None = None


def parse_patch(patch: Sequence[Mapping[str, Any]]) -> tuple[PatchOperation, ...]:
    """Validate a JSON patch document into typed operations."""
    if isinstance(patch, (str, bytes)) or not isinstance(patch, Sequence):
        raise PatchError("invalid_patch", "patch must be a list of operations")
    if len(patch) > MAX_PATCH_OPERATIONS:
        raise PatchError(
            "patch_too_large", f"patch has more than {MAX_PATCH_OPERATIONS} operations"
        )
    operations: list[PatchOperation] = []
    for position, raw in enumerate(patch):
        if not isinstance(raw, Mapping):
            raise PatchError("invalid_patch", f"operation {position} must be an object")
        unknown = set(raw) - {"op", "path", "value", "from"}
        if unknown:
            raise PatchError(
                "invalid_patch", f"operation {position} has unknown members: {sorted(unknown)}"
            )
        op = raw.get("op")
        if not isinstance(op, str) or op not in _PATCH_OPS:
            raise PatchError("invalid_patch", f"operation {position} has unsupported op {op!r}")
        path = raw.get("path")
        if not isinstance(path, str):
            raise PatchError("invalid_patch", f"operation {position} is missing a string path")
        parse_pointer(path)
        value = raw.get("value", _MISSING)
        from_path = raw.get("from")
        if op in _VALUE_OPS and value is _MISSING:
            raise PatchError("invalid_patch", f"operation {position} ({op}) requires a value")
        if op in _FROM_OPS:
            if not isinstance(from_path, str):
                raise PatchError(
                    "invalid_patch", f"operation {position} ({op}) requires a from pointer"
                )
            parse_pointer(from_path)
            from_tokens = parse_pointer(from_path)
            path_tokens = parse_pointer(path)
            if from_tokens and path_tokens[: len(from_tokens)] == from_tokens:
                raise PatchError("invalid_patch", "a value cannot be moved or copied into itself")
        operations.append(
            PatchOperation(op=op, path=path, value=copy.deepcopy(value), from_path=from_path)
        )
    return tuple(operations)


def apply_patch(document: Any, operations: Sequence[PatchOperation]) -> Any:
    """Apply RFC 6902 operations to a deep copy of ``document``.

    Index semantics follow the RFC: ``-`` appends, ``move`` removes before it
    inserts, and ``test`` compares JSON values structurally.
    """
    result = copy.deepcopy(document)
    for operation in operations:
        if operation.op == _ADD:
            _pointer_add(result, parse_pointer(operation.path), copy.deepcopy(operation.value))
        elif operation.op == _REMOVE:
            _pointer_remove(result, parse_pointer(operation.path))
        elif operation.op == _REPLACE:
            _pointer_replace(result, parse_pointer(operation.path), copy.deepcopy(operation.value))
        elif operation.op == _MOVE:
            assert operation.from_path is not None
            moved = _pointer_remove(result, parse_pointer(operation.from_path))
            _pointer_add(result, parse_pointer(operation.path), moved)
        elif operation.op == _COPY:
            assert operation.from_path is not None
            _pointer_add(
                result,
                parse_pointer(operation.path),
                copy.deepcopy(_pointer_get(result, parse_pointer(operation.from_path))),
            )
        elif operation.op == _TEST:
            observed = _pointer_get(result, parse_pointer(operation.path))
            if not _json_equal(observed, operation.value):
                raise PatchError("test_failed", f"test failed at {operation.path}")
    return result


# --------------------------------------------------------------------------- #
# Semantic diff
# --------------------------------------------------------------------------- #


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
    if not _json_equal(base, candidate):
        changes.append(SemanticChange(render_pointer(path) or "/", "replaced", base, candidate))
    return tuple(changes)


# --------------------------------------------------------------------------- #
# Workflow schema validation
# --------------------------------------------------------------------------- #

_SCHEMA_CACHE: dict[str, Any] | None = None


def _workflow_schema_path() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / _WORKFLOW_SCHEMA_RELPATH
        if candidate.is_file():
            return candidate
    return None


def workflow_schema() -> dict[str, Any]:
    """Load ``contracts/workflow-v1.schema.json`` — the only workflow schema."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        path = _workflow_schema_path()
        if path is None:
            raise ProposalPolicyError(
                "SCHEMA_UNAVAILABLE",
                "contracts/workflow-v1.schema.json not found on the import path",
            )
        _SCHEMA_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE


def validate_definition(definition: Any) -> None:
    """Validate a candidate against the shared workflow schema."""
    import jsonschema

    try:
        jsonschema.validate(instance=definition, schema=workflow_schema())
    except jsonschema.ValidationError as exc:
        location = render_pointer(str(part) for part in exc.absolute_path) or "/"
        raise ProposalPolicyError(
            "INVALID_DEFINITION",
            f"candidate violates the workflow schema at {location}: {exc.message}",
            details={"path": location},
        ) from exc


# --------------------------------------------------------------------------- #
# Boundary policy
# --------------------------------------------------------------------------- #

_TRIGGER_PREFIX = "/trigger"
_APPROVER_KEYS = frozenset(
    {
        "approver_user_ids",
        "approval_message",
        "timeout_hours",
        "approval",
        "approvals",
        "approver",
        "approvers",
    }
)
_TARGET_KEYS = frozenset(
    {
        "destination",
        "target",
        "target_id",
        "target_node_id",
        "targets",
        "connector_id",
        "connection_id",
        "endpoint",
        "webhook_url",
        "callback_url",
        "recipient",
        "recipients",
        "external_url",
    }
)
_AUTH_KEYS = frozenset(
    {
        "auth",
        "auth_context",
        "auth_boundary",
        "authentication",
        "authorization",
        "principal",
        "identity",
        "impersonate",
        "impersonation",
        "credential",
        "credentials",
        "credential_id",
        "secret",
        "secrets",
        "secret_ref",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "password",
        "scope",
        "scopes",
        "permission",
        "permissions",
        "role",
        "roles",
        "allowed_roles",
    }
)
_PRIVATE_ID_KEYS = frozenset(
    {
        "tenant_id",
        "user_id",
        "member_id",
        "membership_id",
        "provider_id",
        "credential_id",
        "secret_id",
        "token_id",
        "session_id",
        "owner_id",
        "private_id",
        "private_ids",
        "account_id",
        "external_account_id",
        "connector_instance_id",
    }
)
_PRIVATE_ID_KEY_RE = re.compile(
    r"(^|_)(tenant|user|member|membership|provider|credential|secret|token|session|owner|account|private)_id(s)?$"
)
_ALLOWED_REFERENCE_ID_KEYS = frozenset({"knowledge_base_ids", "knowledge_base_id"})

_HIGH_RISK_SEGMENTS = frozenset({"tool_name", "model", "knowledge_base_ids"})
_HIGH_RISK_PREFIXES = ("/output", "/edges")
_LOW_RISK_TAIL = re.compile(r"^/nodes/[^/]+/name$")

_PII_KEYS = frozenset(
    {"pii", "contains_pii", "personal_data", "pii_fields", "personal_data_fields"}
)
_PII_CLASSIFICATIONS = frozenset({"pii", "sensitive", "restricted", "personal"})


@dataclass(frozen=True, slots=True)
class PolicyViolation:
    code: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ProposalPolicy:
    """Caller-supplied allowlists; empty ``approved_tools`` means "unknown"."""

    approved_tools: frozenset[str] = frozenset()
    private_ids: frozenset[str] = frozenset()


def _segments(path: str) -> tuple[str, ...]:
    return parse_pointer(path)


def _node_map(definition: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(definition, Mapping):
        return {}
    nodes = definition.get("nodes")
    if not isinstance(nodes, list):
        return {}
    return {
        str(node["id"]): node
        for node in nodes
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    }


def _edge_pairs(definition: Any) -> set[tuple[str, str]]:
    if not isinstance(definition, Mapping):
        return set()
    edges = definition.get("edges")
    if not isinstance(edges, list):
        return set()
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if (
            isinstance(edge, Mapping)
            and isinstance(edge.get("from"), str)
            and isinstance(edge.get("to"), str)
        ):
            pairs.add((edge["from"], edge["to"]))
    return pairs


def _reachable(edges: set[tuple[str, str]], start: str, *, reverse: bool) -> set[str]:
    seen: set[str] = set()
    frontier = {start}
    while frontier:
        current = frontier.pop()
        for source, target in edges:
            nxt = source if reverse else target
            head = target if reverse else source
            if head != current or nxt in seen:
                continue
            seen.add(nxt)
            frontier.add(nxt)
    return seen


def _approval_bypass_checks(
    base: Any, candidate: Any, base_nodes: Mapping[str, Mapping[str, Any]]
) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    candidate_nodes = _node_map(candidate)
    base_edges = _edge_pairs(base)
    candidate_edges = _edge_pairs(candidate)
    for node_id, node in sorted(base_nodes.items()):
        if node.get("type") != "approval":
            continue
        kept = candidate_nodes.get(node_id)
        if kept is None:
            violations.append(
                PolicyViolation("APPROVAL_BYPASS", f"/nodes/{node_id}", "approval node removed")
            )
            continue
        if kept.get("type") != "approval":
            violations.append(
                PolicyViolation(
                    "APPROVAL_BYPASS", f"/nodes/{node_id}/type", "approval node retyped"
                )
            )
            continue
        base_exits = {pair for pair in base_edges if pair[0] == node_id}
        candidate_exits = {pair for pair in candidate_edges if pair[0] == node_id}
        if not base_exits.issubset(candidate_exits):
            violations.append(
                PolicyViolation(
                    "APPROVAL_BYPASS", f"/nodes/{node_id}", "approval exit edge removed"
                )
            )
    approval_ids = sorted(
        node_id
        for node_id, node in base_nodes.items()
        if node.get("type") == "approval" and node_id in candidate_nodes
    )
    for node_id in approval_ids:
        ancestors = _reachable(base_edges, node_id, reverse=True)
        descendants = _reachable(base_edges, node_id, reverse=False)
        for edge in sorted(candidate_edges - base_edges):
            source, target = edge
            if source in ancestors and target in descendants:
                violations.append(
                    PolicyViolation(
                        "APPROVAL_BYPASS",
                        "/edges",
                        f"new edge {source} -> {target} skips approval node {node_id}",
                    )
                )
    return violations


def _iter_scalar_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _iter_scalar_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_scalar_values(item)
    else:
        yield value


def check_policy(
    changes: Sequence[SemanticChange],
    *,
    base: Any,
    candidate: Any,
    policy: ProposalPolicy,
) -> tuple[PolicyViolation, ...]:
    """Collect every boundary violation in a stable order."""
    violations: list[PolicyViolation] = []
    base_nodes = _node_map(base)
    candidate_nodes = _node_map(candidate)
    violations.extend(_approval_bypass_checks(base, candidate, base_nodes))
    for change in changes:
        segments = _segments(change.path)
        head = f"/{segments[0]}" if segments else "/"
        tail = segments[-1] if segments else ""
        if head == _TRIGGER_PREFIX:
            violations.append(
                PolicyViolation("TRIGGER_CHANGE", change.path, "trigger changes are not allowed")
            )
        if head == "/nodes" and len(segments) >= 2:
            node_id = segments[1]
            base_type = (base_nodes.get(node_id) or {}).get("type")
            candidate_type = (candidate_nodes.get(node_id) or {}).get("type")
            if (base_type == "approval" or candidate_type == "approval") and tail != "name":
                violations.append(
                    PolicyViolation(
                        "APPROVER_CHANGE", change.path, "approval node attributes are fixed"
                    )
                )
        if tail in _APPROVER_KEYS:
            violations.append(
                PolicyViolation("APPROVER_CHANGE", change.path, "approver configuration is fixed")
            )
        if tail in _TARGET_KEYS:
            violations.append(
                PolicyViolation("TARGET_CHANGE", change.path, "execution targets are fixed")
            )
        if tail in _AUTH_KEYS:
            violations.append(
                PolicyViolation("AUTH_BOUNDARY_CHANGE", change.path, "auth boundary is fixed")
            )

    for node_id, node in sorted(candidate_nodes.items()):
        if node.get("type") != "tool":
            continue
        config = node.get("config")
        tool_name = config.get("tool_name") if isinstance(config, Mapping) else None
        if not isinstance(tool_name, str) or tool_name not in policy.approved_tools:
            violations.append(
                PolicyViolation(
                    "UNAPPROVED_TOOL",
                    f"/nodes/{node_id}/config/tool_name",
                    f"tool {tool_name!r} is not on the approved list",
                )
            )

    for change in changes:
        segments = _segments(change.path)
        tail = segments[-1] if segments else ""
        if (
            (tail in _PRIVATE_ID_KEYS or _PRIVATE_ID_KEY_RE.search(tail))
            and tail not in _ALLOWED_REFERENCE_ID_KEYS
            and change.kind != "removed"
        ):
            violations.append(
                PolicyViolation("PRIVATE_ID", change.path, "private identifiers are not allowed")
            )
        if change.kind == "removed":
            continue
        for value in _iter_scalar_values(change.new):
            if isinstance(value, str) and value in policy.private_ids:
                violations.append(
                    PolicyViolation(
                        "PRIVATE_ID", change.path, "private identifier value is not allowed"
                    )
                )
                break
    return tuple(violations)


# --------------------------------------------------------------------------- #
# Risk classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: Literal["low", "medium", "high"]
    pii: bool
    required_approvals: int
    requires_manual_shadow: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "pii": self.pii,
            "required_approvals": self.required_approvals,
            "requires_manual_shadow": self.requires_manual_shadow,
        }


def contains_pii(definition: Any) -> bool:
    """True when the definition declares personal or sensitive data handling."""
    if isinstance(definition, Mapping):
        for key, value in definition.items():
            folded = str(key).casefold()
            if folded in _PII_KEYS and bool(value):
                return True
            if (
                folded == "data_classification"
                and isinstance(value, str)
                and value.casefold() in _PII_CLASSIFICATIONS
            ):
                return True
            if contains_pii(value):
                return True
    elif isinstance(definition, list):
        return any(contains_pii(item) for item in definition)
    return False


def classification_for(changes: Sequence[SemanticChange], *, candidate: Any) -> RiskAssessment:
    """Derive the risk class, approval count and shadow requirement."""
    level: Literal["low", "medium", "high"] = "low"
    for change in changes:
        segments = _segments(change.path)
        head = f"/{segments[0]}" if segments else "/"
        if head in _HIGH_RISK_PREFIXES or head == "/nodes" and len(segments) == 2:
            level = "high"
            break
        if head == "/nodes" and len(segments) >= 3 and segments[-1] in _HIGH_RISK_SEGMENTS:
            level = "high"
            break
        if not _LOW_RISK_TAIL.match(change.path):
            level = "medium"
    pii = contains_pii(candidate)
    if pii and level == "low":
        level = "medium"
    required = 1 if level == "low" and not pii else 2
    return RiskAssessment(
        level=level,
        pii=pii,
        required_approvals=required,
        requires_manual_shadow=not (level == "low" and not pii),
    )


# --------------------------------------------------------------------------- #
# Reviews and approvals
# --------------------------------------------------------------------------- #


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ReviewVote:
    reviewer_user_id: int
    decision: ReviewDecision


@dataclass(frozen=True, slots=True)
class ApprovalState:
    outcome: ApprovalOutcome
    approvals: int
    rejections: int
    required: int
    remaining: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "required": self.required,
            "remaining": self.remaining,
        }


def evaluate_approvals(
    votes: Sequence[ReviewVote], *, creator_user_id: int, required_approvals: int
) -> ApprovalState:
    """Apply the approval gate: one vote per reviewer, reject wins, no self-review."""
    if required_approvals < 1:
        raise ProposalPolicyError(
            "INVALID_APPROVAL_REQUIREMENT", "required approvals must be at least one"
        )
    seen: dict[int, ReviewDecision] = {}
    for vote in votes:
        if vote.reviewer_user_id == creator_user_id:
            raise ProposalPolicyError(
                "CREATOR_SELF_REVIEW", "the proposal creator cannot review it"
            )
        if vote.reviewer_user_id in seen:
            raise ProposalPolicyError("DUPLICATE_REVIEW", "a reviewer may only vote once")
        seen[vote.reviewer_user_id] = vote.decision
    approvals = sum(1 for decision in seen.values() if decision is ReviewDecision.APPROVED)
    rejections = sum(1 for decision in seen.values() if decision is ReviewDecision.REJECTED)
    if rejections:
        outcome = ApprovalOutcome.REJECTED
    elif approvals >= required_approvals:
        outcome = ApprovalOutcome.APPROVED
    else:
        outcome = ApprovalOutcome.PENDING
    return ApprovalState(
        outcome=outcome,
        approvals=approvals,
        rejections=rejections,
        required=required_approvals,
        remaining=max(required_approvals - approvals, 0),
    )


# --------------------------------------------------------------------------- #
# Canary bucketing
# --------------------------------------------------------------------------- #


def canary_bucket(stable_key: str) -> int:
    """Deterministic bucket in ``[0, 10000)``.

    Spec: ``int.from_bytes(sha256(key.encode("utf-8")).digest()[:8], "big") % 10000``.
    The first eight digest bytes are read as one big-endian unsigned integer, so
    any language with SHA-256 reproduces the same bucket for the same key.
    """
    digest = hashlib.sha256(stable_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % CANARY_BUCKET_MODULUS


def canary_key(*parts: object) -> str:
    """Stable canary key: unit-separator joined fields of a single execution."""
    return "\x1f".join(str(part) for part in parts)


def is_canary_selected(bucket: int, ratio_basis_points: int) -> bool:
    """True when a bucket falls inside the candidate share (basis points)."""
    if not 0 <= ratio_basis_points <= CANARY_BUCKET_MODULUS:
        raise ProposalPolicyError("CANARY_RATIO_INVALID", "canary ratio must be within 0..10000")
    return bucket < ratio_basis_points


def canary_lane(stable_key: str, ratio_basis_points: int) -> Literal["candidate", "baseline"]:
    return (
        "candidate"
        if is_canary_selected(canary_bucket(stable_key), ratio_basis_points)
        else "baseline"
    )


# --------------------------------------------------------------------------- #
# Shadow proof and canary gates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ShadowRunRecord:
    run_id: str
    settled: bool
    replay_only: bool
    live_side_effects: int = 0


@dataclass(frozen=True, slots=True)
class ShadowProof:
    complete: bool
    settled_runs: int
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "settled_runs": self.settled_runs,
            "failures": list(self.failures),
        }


def evaluate_shadow_proof(
    runs: Sequence[ShadowRunRecord], *, min_settled_runs: int = SHADOW_MIN_SETTLED_RUNS
) -> ShadowProof:
    """Replay-only shadow proof: settled runs, no live side effects ever."""
    settled = [run for run in runs if run.settled]
    failures: list[str] = []
    if not settled:
        failures.append("no_settled_shadow_runs")
    elif len(settled) < min_settled_runs:
        failures.append("insufficient_shadow_runs")
    if any(not run.replay_only for run in runs):
        failures.append("shadow_not_replay_only")
    if any(run.live_side_effects for run in runs):
        failures.append("shadow_live_side_effects")
    return ShadowProof(complete=not failures, settled_runs=len(settled), failures=tuple(failures))


@dataclass(frozen=True, slots=True)
class PhaseMetrics:
    settled_runs: int
    success_rate: float
    p95_latency_ms: float
    avg_tokens: float
    safety_violations: int = 0


@dataclass(frozen=True, slots=True)
class CanaryEvidence:
    window_start: int
    window_end: int
    baseline: PhaseMetrics
    candidate: PhaseMetrics

    @property
    def window_seconds(self) -> int:
        return max(self.window_end - self.window_start, 0)

    @property
    def window_full_days(self) -> int:
        return self.window_seconds // CANARY_FULL_DAY_SECONDS


@dataclass(frozen=True, slots=True)
class GateThresholds:
    min_full_days: int = CANARY_MIN_FULL_DAYS
    min_settled_runs: int = CANARY_MIN_SETTLED_RUNS
    max_success_drop: float = 0.02
    min_success_rate: float = 0.95
    max_latency_ratio: float = 1.20
    max_token_ratio: float = 1.25


DEFAULT_GATE_THRESHOLDS = GateThresholds()


@dataclass(frozen=True, slots=True)
class GateVerdict:
    passed: bool
    failures: tuple[str, ...]
    safety_stop: bool
    full_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "safety_stop": self.safety_stop,
            "full_days": self.full_days,
        }


def evaluate_canary_gates(
    evidence: CanaryEvidence, thresholds: GateThresholds = DEFAULT_GATE_THRESHOLDS
) -> GateVerdict:
    """Success / latency / token / safety gates plus the 7-day, 100-run floor."""
    failures: list[str] = []
    safety_stop = evidence.candidate.safety_violations > 0
    if safety_stop:
        failures.append("safety_violation")
    full_days = evidence.window_full_days
    if full_days < thresholds.min_full_days:
        failures.append("insufficient_window")
    if evidence.baseline.settled_runs < thresholds.min_settled_runs:
        failures.append("insufficient_baseline_samples")
    if evidence.candidate.settled_runs < thresholds.min_settled_runs:
        failures.append("insufficient_candidate_samples")
    if evidence.baseline.settled_runs and evidence.candidate.settled_runs:
        if evidence.candidate.success_rate < thresholds.min_success_rate:
            failures.append("success_rate_below_floor")
        if (
            evidence.baseline.success_rate - evidence.candidate.success_rate
            > thresholds.max_success_drop
        ):
            failures.append("success_rate_regression")
        if evidence.baseline.p95_latency_ms > 0:
            ratio = evidence.candidate.p95_latency_ms / evidence.baseline.p95_latency_ms
            if ratio > thresholds.max_latency_ratio:
                failures.append("latency_regression")
        if evidence.baseline.avg_tokens > 0:
            ratio = evidence.candidate.avg_tokens / evidence.baseline.avg_tokens
            if ratio > thresholds.max_token_ratio:
                failures.append("token_regression")
    return GateVerdict(
        passed=not failures, failures=tuple(failures), safety_stop=safety_stop, full_days=full_days
    )


# --------------------------------------------------------------------------- #
# Promotion state machine
# --------------------------------------------------------------------------- #


class ProposalStatus(StrEnum):
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SHADOW = "shadow"
    CANARY = "canary"
    APPLIED = "applied"
    ABORTED = "aborted"
    SUPERSEDED = "superseded"
    STALE = "stale"


OPEN_STATUSES = frozenset(
    {
        ProposalStatus.UNDER_REVIEW,
        ProposalStatus.APPROVED,
        ProposalStatus.SHADOW,
        ProposalStatus.CANARY,
    }
)
# ``PENDING_STATUSES`` is the "one open proposal per workflow" gate: shadow and
# canary proposals are already promoted, so a new idea may be drafted while they
# run, and applying one supersedes the others via CAS on the workflow revision.
PENDING_STATUSES = frozenset({ProposalStatus.UNDER_REVIEW, ProposalStatus.APPROVED})
TERMINAL_STATUSES = frozenset(ProposalStatus) - OPEN_STATUSES


class PromotionAction(StrEnum):
    START_SHADOW = "start_shadow"
    START_CANARY = "start_canary"
    APPLY = "apply"
    ABORT = "abort"


_TRANSITIONS: dict[PromotionAction, dict[ProposalStatus, ProposalStatus]] = {
    PromotionAction.START_SHADOW: {ProposalStatus.APPROVED: ProposalStatus.SHADOW},
    PromotionAction.START_CANARY: {
        ProposalStatus.APPROVED: ProposalStatus.CANARY,
        ProposalStatus.SHADOW: ProposalStatus.CANARY,
    },
    PromotionAction.APPLY: {ProposalStatus.CANARY: ProposalStatus.APPLIED},
    PromotionAction.ABORT: {
        ProposalStatus.UNDER_REVIEW: ProposalStatus.ABORTED,
        ProposalStatus.APPROVED: ProposalStatus.ABORTED,
        ProposalStatus.SHADOW: ProposalStatus.ABORTED,
        ProposalStatus.CANARY: ProposalStatus.ABORTED,
    },
}


def transition_for(action: PromotionAction, status: ProposalStatus) -> ProposalStatus:
    """Target status for an action, or a policy error when the transition is invalid."""
    target = _TRANSITIONS[action].get(status)
    if target is None:
        raise ProposalPolicyError(
            "INVALID_STATE",
            f"action {action.value} is not allowed from status {status.value}",
            details={"action": action.value, "status": status.value},
        )
    return target


def canary_admission(
    *,
    status: ProposalStatus,
    requires_manual_shadow: bool,
    shadow: ShadowProof | None,
    ratio_basis_points: int,
) -> None:
    """Validate the shadow evidence gate before candidate traffic starts."""
    if (status is ProposalStatus.SHADOW or requires_manual_shadow) and (
        shadow is None or not shadow.complete
    ):
        failures = list(shadow.failures) if shadow is not None else ["shadow_not_run"]
        raise ProposalPolicyError(
            "SHADOW_PROOF_REQUIRED",
            "manual replay-only shadow evidence is required before canary traffic",
            details={"failures": failures},
        )
    if not 1 <= ratio_basis_points <= CANARY_BUCKET_MODULUS:
        raise ProposalPolicyError("CANARY_RATIO_INVALID", "canary ratio must be within 1..10000")


# --------------------------------------------------------------------------- #
# Compiled proposal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CompiledProposal:
    base_definition: dict[str, Any]
    candidate_definition: dict[str, Any]
    base_content_hash: str
    candidate_content_hash: str
    changes: tuple[SemanticChange, ...]
    risk: RiskAssessment
    policy_violations: tuple[PolicyViolation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_content_hash": self.base_content_hash,
            "candidate_content_hash": self.candidate_content_hash,
            "changes": [change.to_dict() for change in self.changes],
            "risk": self.risk.to_dict(),
        }


def compile_proposal(
    base_definition: Mapping[str, Any],
    patch: Sequence[Mapping[str, Any]],
    *,
    policy: ProposalPolicy,
) -> CompiledProposal:
    """Apply a patch to the canonical base and prove the candidate.

    The compiled candidate is only returned when the *final* document passes the
    schema, the semantic boundary policy and the tool allowlist.  Anything else
    raises :class:`ProposalPolicyError` with a stable code.
    """
    if not policy.approved_tools:
        raise ProposalPolicyError(
            ALLOWLIST_UNAVAILABLE,
            "the tenant tool allowlist is not configured; proposals cannot be evaluated",
        )
    operations = parse_patch(patch)
    candidate = apply_patch(base_definition, operations)
    validate_definition(candidate)
    changes = semantic_diff(base_definition, candidate)
    if not changes:
        raise ProposalPolicyError("NO_SEMANTIC_CHANGE", "the patch does not change the workflow")
    violations = check_policy(changes, base=base_definition, candidate=candidate, policy=policy)
    if violations:
        first = violations[0]
        raise ProposalPolicyError(
            first.code,
            first.detail,
            details={"violations": [item.to_dict() for item in violations]},
        )
    risk = classification_for(changes, candidate=candidate)
    return CompiledProposal(
        base_definition=copy.deepcopy(dict(base_definition)),
        candidate_definition=candidate,
        base_content_hash=definition_hash(base_definition),
        candidate_content_hash=definition_hash(candidate),
        changes=changes,
        risk=risk,
    )


# --------------------------------------------------------------------------- #
# Store boundary
# --------------------------------------------------------------------------- #


class ProposalNotFoundError(LookupError):
    """The workflow or proposal is not visible inside the caller's tenant."""


class ProposalConflictError(RuntimeError):
    """A uniqueness or compare-and-swap constraint refused the write."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ProposalActor:
    """The authenticated tenant actor driving a proposal operation."""

    user_id: int
    membership_id: str | None = None
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class NewProposal:
    workflow_id: str
    expect_revision: int
    patch: tuple[Mapping[str, Any], ...]
    change_summary: str
    actor: ProposalActor


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    workflow_id: str
    workflow_revision: int
    base_version_id: str
    base_content_hash: str
    candidate_version_id: str
    candidate_content_hash: str
    status: ProposalStatus
    risk_level: Literal["low", "medium", "high"]
    pii_involved: bool
    required_approvals: int
    requires_manual_shadow: bool
    change_summary: str
    changes: tuple[SemanticChange, ...]
    created_by_user_id: int
    created_by_membership_id: str | None
    created_at: int
    updated_at: int
    canary_ratio_bp: int | None = None
    canary_started_at: int | None = None
    canary_stopped_at: int | None = None
    canary_stop_reason: str | None = None
    applied_version_id: str | None = None
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class NewReview:
    proposal_id: str
    reviewer_user_id: int
    reviewer_membership_id: str | None
    decision: ReviewDecision
    comment: str
    created_at: int


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    review_id: str
    proposal_id: str
    reviewer_user_id: int
    reviewer_membership_id: str | None
    decision: ReviewDecision
    comment: str
    created_at: int


@dataclass(frozen=True, slots=True)
class ShadowRunRow:
    """One replay record produced by the shadow runner."""

    run_id: str
    settled: bool
    replay_only: bool
    live_side_effects: int
    evidence_hash: str
    created_at: int

    def to_record(self) -> ShadowRunRecord:
        return ShadowRunRecord(
            run_id=self.run_id,
            settled=self.settled,
            replay_only=self.replay_only,
            live_side_effects=self.live_side_effects,
        )


@dataclass(frozen=True, slots=True)
class NewEvaluation:
    phase: Literal["shadow", "canary"]
    window_start: int
    window_end: int
    baseline: PhaseMetrics
    candidate: PhaseMetrics
    created_at: int


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    evaluation_id: str
    phase: Literal["shadow", "canary"]
    window_start: int
    window_end: int
    baseline: PhaseMetrics
    candidate: PhaseMetrics
    verdict: GateVerdict
    created_at: int


@dataclass(frozen=True, slots=True)
class WorkflowPointer:
    workflow_id: str
    revision: int
    active_version_id: str | None
    active_definition_hash: str | None


CompileFn = Callable[[Mapping[str, Any]], CompiledProposal]


class ProposalStore(Protocol):
    """Tenant-scoped persistence used by the service.

    Implementations run every method inside ``workbuddy_transaction``; the
    tenant identity never comes from arguments the caller could forge.
    """

    def create_proposal(self, request: NewProposal, *, compile: CompileFn) -> ProposalRecord: ...

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None: ...

    def list_proposals(
        self,
        *,
        workflow_id: str | None = None,
        status: ProposalStatus | None = None,
        limit: int = 100,
    ) -> list[ProposalRecord]: ...

    def list_reviews(self, proposal_id: str) -> list[ReviewRecord]: ...

    def add_review(self, review: NewReview) -> ReviewRecord: ...

    def list_shadow_runs(self, proposal_id: str) -> list[ShadowRunRow]: ...

    def add_shadow_run(self, proposal_id: str, run: ShadowRunRow) -> ShadowRunRow: ...

    def list_evaluations(self, proposal_id: str) -> list[EvaluationRow]: ...

    def add_evaluation(
        self, proposal_id: str, evaluation: NewEvaluation, verdict: GateVerdict
    ) -> EvaluationRow: ...

    def workflow_pointer(self, workflow_id: str) -> WorkflowPointer | None: ...

    def active_canary(self, workflow_id: str) -> ProposalRecord | None: ...

    def transition(
        self,
        proposal_id: str,
        *,
        expect_status: ProposalStatus,
        expect_revision: int,
        status: ProposalStatus,
        fields: Mapping[str, Any],
    ) -> ProposalRecord | None: ...

    def apply_promotion(
        self, proposal_id: str, *, expect_revision: int, actor_user_id: int
    ) -> ProposalRecord | None: ...


# --------------------------------------------------------------------------- #
# Application service
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProposalView:
    """Client projection: governance data plus the frozen semantic evidence."""

    proposal: ProposalRecord
    stale: bool = False
    reviews: tuple[ReviewRecord, ...] = ()
    shadow_proof: ShadowProof | None = None
    evaluations: tuple[EvaluationRow, ...] = ()
    last_gate: GateVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        record = self.proposal
        payload: dict[str, Any] = {
            "proposal_id": record.proposal_id,
            "workflow_id": record.workflow_id,
            "workflow_revision": record.workflow_revision,
            "base_version_id": record.base_version_id,
            "base_content_hash": record.base_content_hash,
            "candidate_version_id": record.candidate_version_id,
            "candidate_content_hash": record.candidate_content_hash,
            "status": record.status.value,
            "status_reason": record.status_reason,
            "risk_level": record.risk_level,
            "pii_involved": record.pii_involved,
            "required_approvals": record.required_approvals,
            "requires_manual_shadow": record.requires_manual_shadow,
            "change_summary": record.change_summary,
            "changes": [change.to_dict() for change in record.changes],
            "created_by_user_id": record.created_by_user_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "canary_ratio_bp": record.canary_ratio_bp,
            "canary_started_at": record.canary_started_at,
            "canary_stopped_at": record.canary_stopped_at,
            "canary_stop_reason": record.canary_stop_reason,
            "applied_version_id": record.applied_version_id,
            "stale": self.stale,
        }
        if self.reviews:
            payload["reviews"] = [
                {
                    "review_id": review.review_id,
                    "reviewer_user_id": review.reviewer_user_id,
                    "decision": review.decision.value,
                    "comment": review.comment,
                    "created_at": review.created_at,
                }
                for review in self.reviews
            ]
        if self.shadow_proof is not None:
            payload["shadow_proof"] = self.shadow_proof.to_dict()
        if self.evaluations:
            payload["evaluations"] = [
                {
                    "evaluation_id": row.evaluation_id,
                    "phase": row.phase,
                    "window_start": row.window_start,
                    "window_end": row.window_end,
                    "verdict": row.verdict.to_dict(),
                    "baseline": _metrics_dict(row.baseline),
                    "candidate": _metrics_dict(row.candidate),
                }
                for row in self.evaluations
            ]
        if self.last_gate is not None:
            payload["last_gate"] = self.last_gate.to_dict()
        return payload


def _metrics_dict(metrics: PhaseMetrics) -> dict[str, Any]:
    return {
        "settled_runs": metrics.settled_runs,
        "success_rate": metrics.success_rate,
        "p95_latency_ms": metrics.p95_latency_ms,
        "avg_tokens": metrics.avg_tokens,
        "safety_violations": metrics.safety_violations,
    }


class WorkBuddyProposalsService:
    """Compile, review and promote improvement proposals.

    All tenancy, locking and compare-and-swap work happens in the store; this
    layer owns the deterministic policy: risk classes, approval arithmetic,
    shadow proofs, gates and the promotion state machine.
    """

    def __init__(
        self,
        store: ProposalStore,
        *,
        policy: ProposalPolicy,
        thresholds: GateThresholds = DEFAULT_GATE_THRESHOLDS,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._store = store
        self._policy = policy
        self._thresholds = thresholds
        self._now = now

    # -- creation ---------------------------------------------------------- #

    def create(
        self,
        *,
        workflow_id: str,
        patch: Sequence[Mapping[str, Any]],
        change_summary: str,
        actor: ProposalActor,
        expect_revision: int,
    ) -> ProposalView:
        if not self._policy.approved_tools:
            raise ProposalPolicyError(
                ALLOWLIST_UNAVAILABLE,
                "the tenant tool allowlist is not configured; proposals cannot be evaluated",
            )
        request = NewProposal(
            workflow_id=workflow_id,
            expect_revision=expect_revision,
            patch=tuple(patch),
            change_summary=change_summary,
            actor=actor,
        )

        def compile_with_base(base_definition: Mapping[str, Any]) -> CompiledProposal:
            return compile_proposal(base_definition, patch, policy=self._policy)

        try:
            record = self._store.create_proposal(request, compile=compile_with_base)
        except ProposalNotFoundError as exc:
            raise ProposalPolicyError("WORKFLOW_NOT_FOUND", str(exc)) from exc
        except ProposalConflictError as exc:
            raise ProposalPolicyError(exc.code, exc.message) from exc
        return self._view(record)

    # -- reads ------------------------------------------------------------- #

    def list(
        self,
        *,
        workflow_id: str | None = None,
        status: ProposalStatus | None = None,
        limit: int = 100,
    ) -> list[ProposalView]:
        records = self._store.list_proposals(workflow_id=workflow_id, status=status, limit=limit)
        pointers: dict[str, WorkflowPointer | None] = {}
        views: list[ProposalView] = []
        for record in records:
            if record.workflow_id not in pointers:
                pointers[record.workflow_id] = self._store.workflow_pointer(record.workflow_id)
            views.append(self._view(record, pointer=pointers[record.workflow_id]))
        return views

    def get(self, proposal_id: str) -> ProposalView:
        record = self._refresh_staleness(self._require(proposal_id))
        return self._view(
            record, detail=True, pointer=self._store.workflow_pointer(record.workflow_id)
        )

    # -- decisions --------------------------------------------------------- #

    def decide(
        self,
        proposal_id: str,
        *,
        reviewer: ProposalActor,
        decision: ReviewDecision,
        comment: str,
    ) -> ProposalView:
        record = self._refresh_staleness(self._require(proposal_id))
        if record.status not in OPEN_STATUSES:
            raise ProposalPolicyError(
                "INVALID_STATE",
                f"proposal is {record.status.value}; decisions are not accepted",
            )
        if reviewer.user_id == record.created_by_user_id:
            raise ProposalPolicyError(
                "CREATOR_SELF_REVIEW", "the proposal creator cannot review it"
            )
        if decision is ReviewDecision.APPROVED and record.status not in (
            ProposalStatus.UNDER_REVIEW,
            ProposalStatus.APPROVED,
        ):
            raise ProposalPolicyError(
                "INVALID_STATE",
                "approvals are only accepted before candidate traffic starts",
            )
        review = NewReview(
            proposal_id=proposal_id,
            reviewer_user_id=reviewer.user_id,
            reviewer_membership_id=reviewer.membership_id,
            decision=decision,
            comment=comment,
            created_at=self._now(),
        )
        try:
            self._store.add_review(review)
        except ProposalConflictError as exc:
            raise ProposalPolicyError(exc.code, exc.message) from exc
        votes = [
            ReviewVote(reviewer_user_id=row.reviewer_user_id, decision=row.decision)
            for row in self._store.list_reviews(proposal_id)
        ]
        state = evaluate_approvals(
            votes,
            creator_user_id=record.created_by_user_id,
            required_approvals=record.required_approvals,
        )
        if state.outcome is ApprovalOutcome.REJECTED:
            record = self._transition(
                record, ProposalStatus.REJECTED, self._stop_fields(record, "rejected")
            )
        elif (
            state.outcome is ApprovalOutcome.APPROVED
            and record.status is ProposalStatus.UNDER_REVIEW
        ):
            record = self._transition(record, ProposalStatus.APPROVED, {"status_reason": None})
        else:
            record = self._require(proposal_id)
        return self._view(
            record, detail=True, pointer=self._store.workflow_pointer(record.workflow_id)
        )

    # -- promotion --------------------------------------------------------- #

    def promote(
        self,
        proposal_id: str,
        *,
        action: PromotionAction,
        if_match_revision: int,
        actor: ProposalActor,
        ratio_basis_points: int = 0,
    ) -> ProposalView:
        record = self._refresh_staleness(self._require(proposal_id))
        if record.status is ProposalStatus.STALE:
            raise ProposalPolicyError(
                "PROPOSAL_STALE",
                "the workflow baseline changed; reopen the proposal against the new base",
            )
        if if_match_revision != record.workflow_revision:
            raise ProposalPolicyError(
                "STALE_REVISION",
                "the workflow revision changed; reload the proposal and retry",
                details={"workflow_revision": record.workflow_revision},
            )
        if action is PromotionAction.ABORT:
            record = self._transition(
                record, ProposalStatus.ABORTED, self._stop_fields(record, "aborted")
            )
        elif action is PromotionAction.START_SHADOW:
            target = transition_for(action, record.status)
            record = self._transition(record, target, {"status_reason": None})
        elif action is PromotionAction.START_CANARY:
            target = transition_for(action, record.status)
            proof = self._shadow_proof(proposal_id)
            canary_admission(
                status=record.status,
                requires_manual_shadow=record.requires_manual_shadow,
                shadow=proof,
                ratio_basis_points=ratio_basis_points,
            )
            record = self._transition(
                record,
                target,
                {
                    "status_reason": None,
                    "canary_ratio_bp": ratio_basis_points,
                    "canary_started_at": self._now(),
                    "canary_stopped_at": None,
                    "canary_stop_reason": None,
                },
            )
        elif action is PromotionAction.APPLY:
            transition_for(action, record.status)
            verdict = self._latest_gate(proposal_id)
            if verdict is None:
                raise ProposalPolicyError(
                    "GATES_NOT_PASSED",
                    "no canary evaluation has been recorded",
                    details={"failures": ["no_canary_evaluation"]},
                )
            if not verdict.passed:
                raise ProposalPolicyError(
                    "GATES_NOT_PASSED",
                    "canary gates have not passed",
                    details={"failures": list(verdict.failures)},
                )
            try:
                updated = self._store.apply_promotion(
                    proposal_id, expect_revision=if_match_revision, actor_user_id=actor.user_id
                )
            except ProposalConflictError as exc:
                raise ProposalPolicyError(exc.code, exc.message) from exc
            if updated is None:
                raise ProposalPolicyError(
                    "STALE_REVISION", "the workflow revision changed during promotion; retry"
                )
            record = updated
        return self._view(
            record, detail=True, pointer=self._store.workflow_pointer(record.workflow_id)
        )

    # -- evidence ---------------------------------------------------------- #

    def record_shadow_run(self, proposal_id: str, *, run: ShadowRunRow) -> ProposalView:
        record = self._require(proposal_id)
        if record.status not in OPEN_STATUSES:
            raise ProposalPolicyError(
                "INVALID_STATE", f"proposal is {record.status.value}; shadow runs are closed"
            )
        self._store.add_shadow_run(proposal_id, run)
        return self._view(
            record, detail=True, pointer=self._store.workflow_pointer(record.workflow_id)
        )

    def record_evaluation(self, proposal_id: str, *, evaluation: NewEvaluation) -> ProposalView:
        record = self._require(proposal_id)
        if record.status not in OPEN_STATUSES:
            raise ProposalPolicyError(
                "INVALID_STATE", f"proposal is {record.status.value}; evaluations are closed"
            )
        if evaluation.phase not in ("shadow", "canary"):
            raise ProposalPolicyError(
                "INVALID_EVALUATION", "evaluation phase must be shadow or canary"
            )
        verdict = evaluate_canary_gates(
            CanaryEvidence(
                window_start=evaluation.window_start,
                window_end=evaluation.window_end,
                baseline=evaluation.baseline,
                candidate=evaluation.candidate,
            ),
            self._thresholds,
        )
        self._store.add_evaluation(proposal_id, evaluation, verdict)
        if verdict.safety_stop and record.status is ProposalStatus.CANARY:
            record = self._transition(
                record, ProposalStatus.ABORTED, self._stop_fields(record, "safety_violation")
            )
        return self._view(
            record, detail=True, pointer=self._store.workflow_pointer(record.workflow_id)
        )

    # -- routing ----------------------------------------------------------- #

    def lane_for(self, workflow_id: str, stable_key: str) -> Literal["candidate", "baseline"]:
        """Route one execution: candidate traffic only while a canary is live."""
        active = self._store.active_canary(workflow_id)
        if active is None or active.canary_ratio_bp is None or active.canary_stopped_at is not None:
            return "baseline"
        return canary_lane(stable_key, active.canary_ratio_bp)

    # -- internals --------------------------------------------------------- #

    def _require(self, proposal_id: str) -> ProposalRecord:
        record = self._store.get_proposal(proposal_id)
        if record is None:
            raise ProposalNotFoundError(proposal_id)
        return record

    def _refresh_staleness(self, record: ProposalRecord) -> ProposalRecord:
        if record.status not in OPEN_STATUSES:
            return record
        pointer = self._store.workflow_pointer(record.workflow_id)
        if pointer is None:
            raise ProposalNotFoundError(record.proposal_id)
        unchanged = pointer.revision == record.workflow_revision and (
            pointer.active_definition_hash in (None, record.base_content_hash)
        )
        if unchanged:
            return record
        updated = self._store.transition(
            record.proposal_id,
            expect_status=record.status,
            expect_revision=record.workflow_revision,
            status=ProposalStatus.STALE,
            fields=self._stop_fields(record, "stale"),
        )
        return updated or record

    def _stop_fields(self, record: ProposalRecord, reason: str) -> dict[str, Any]:
        fields: dict[str, Any] = {"status_reason": reason}
        if record.canary_started_at is not None and record.canary_stopped_at is None:
            fields["canary_stopped_at"] = self._now()
            fields["canary_stop_reason"] = reason
        return fields

    def _transition(
        self, record: ProposalRecord, status: ProposalStatus, fields: Mapping[str, Any]
    ) -> ProposalRecord:
        updated = self._store.transition(
            record.proposal_id,
            expect_status=record.status,
            expect_revision=record.workflow_revision,
            status=status,
            fields=fields,
        )
        if updated is None:
            raise ProposalPolicyError(
                "PROPOSAL_CHANGED", "the proposal changed concurrently; reload and retry"
            )
        return updated

    def _shadow_proof(self, proposal_id: str) -> ShadowProof:
        runs = [row.to_record() for row in self._store.list_shadow_runs(proposal_id)]
        return evaluate_shadow_proof(runs)

    def _latest_gate(self, proposal_id: str) -> GateVerdict | None:
        rows = [row for row in self._store.list_evaluations(proposal_id) if row.phase == "canary"]
        return rows[-1].verdict if rows else None

    def _view(
        self,
        record: ProposalRecord,
        *,
        detail: bool = False,
        pointer: WorkflowPointer | None = None,
    ) -> ProposalView:
        if pointer is None:
            pointer = self._store.workflow_pointer(record.workflow_id)
        stale = bool(
            pointer is not None
            and record.status in OPEN_STATUSES
            and (
                pointer.revision != record.workflow_revision
                or pointer.active_definition_hash not in (None, record.base_content_hash)
            )
        )
        if not detail:
            # The list carries the review record too: the contract gives workflow
            # managers the full governance picture there, and the API layer strips
            # it for everyone else.
            return ProposalView(
                proposal=record,
                stale=stale,
                reviews=tuple(self._store.list_reviews(record.proposal_id)),
            )
        reviews = tuple(self._store.list_reviews(record.proposal_id))
        evaluations = tuple(self._store.list_evaluations(record.proposal_id))
        return ProposalView(
            proposal=record,
            stale=stale,
            reviews=reviews,
            shadow_proof=self._shadow_proof(record.proposal_id),
            evaluations=evaluations,
            last_gate=self._latest_gate(record.proposal_id),
        )
