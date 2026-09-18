"""Compile WorkBuddy workflow drafts into one canonical, immutable definition.

This module is the only implementation of workflow definition validation:

* structural validation against ``contracts/workflow-v1.schema.json`` (Draft 7,
  UUID format checking, local ``#/definitions`` references only — a remote
  reference makes the schema unusable instead of being fetched);
* default normalization, so an accepted draft has exactly one canonical form;
* DAG rules: unique node ids, known edge endpoints, no self-loops or duplicate
  edges, exactly one entry node, every node reachable, no cycles;
* condition and approval edge rules;
* unique ``save_as`` names, path-safe upstream references and restricted
  one-pass templates;
* bounded CEL parse checks through :mod:`octop.infra.workbuddy.cel_sandbox`;
* tool / model / knowledge-base / approver semantic checks through the
  :class:`WorkflowSemanticResolver` interface (never faked: when a definition
  needs a resolver and none is available the compile fails closed).

The repository persists exactly the ``definition`` returned together with
``definition_sha256`` (SHA-256 over deterministic canonical JSON), so a stored
version can be re-verified and never re-interpreted.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from octop.infra.workbuddy.cel_sandbox import CELSandboxError, evaluate_cel

WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_COMPILER_VERSION = "workbuddy-workflow-compiler/1"
WORKFLOW_SCHEMA_FILENAME = "workflow-v1.schema.json"
WORKFLOW_SCHEMA_ENV = "WORKBUDDY_WORKFLOW_SCHEMA_PATH"

MAX_TEMPLATE_PLACEHOLDERS = 32
MAX_REFERENCE_LENGTH = 200

NODE_TYPES = frozenset({"tool", "llm", "condition", "approval", "transform"})
ACTIVATABLE_VERSION_ORIGINS = frozenset({"save", "rollback", "promotion", "import"})
CANDIDATE_VERSION_ORIGINS = frozenset({"proposal"})
VERSION_ORIGINS = ACTIVATABLE_VERSION_ORIGINS | CANDIDATE_VERSION_ORIGINS

# Compiler-level codes.  Codes that name an :class:`~octop.infra.errors.ErrorCode`
# member surface with that exact member; everything else maps to the workflow
# validation error at the API boundary.
WORKFLOW_SCHEMA_INVALID = "WORKFLOW_SCHEMA_INVALID"
WORKFLOW_DEPENDENCY_UNAVAILABLE = "WORKBUDDY_DEPENDENCY_UNAVAILABLE"
WORKFLOW_MODEL_NOT_CONFIGURED = "WORKBUDDY_MODEL_NOT_CONFIGURED"
WORKFLOW_NODE_DUPLICATE_ID = "WORKFLOW_NODE_DUPLICATE_ID"
WORKFLOW_SAVE_AS_DUPLICATE = "WORKFLOW_SAVE_AS_DUPLICATE"
WORKFLOW_EDGE_UNKNOWN_NODE = "WORKFLOW_EDGE_UNKNOWN_NODE"
WORKFLOW_EDGE_DUPLICATE = "WORKFLOW_EDGE_DUPLICATE"
WORKFLOW_EDGE_SELF_LOOP = "WORKFLOW_EDGE_SELF_LOOP"
WORKFLOW_EDGE_UNEXPECTED_WHEN = "WORKFLOW_EDGE_UNEXPECTED_WHEN"
WORKFLOW_CONDITION_EDGES = "WORKFLOW_CONDITION_EDGES"
WORKFLOW_APPROVAL_EDGES = "WORKFLOW_APPROVAL_EDGES"
WORKFLOW_APPROVAL_TARGET = "WORKFLOW_APPROVAL_TARGET"
WORKFLOW_FAN_OUT = "WORKFLOW_FAN_OUT"
WORKFLOW_ENTRY_COUNT = "WORKFLOW_ENTRY_COUNT"
WORKFLOW_CYCLE = "WORKFLOW_CYCLE"
WORKFLOW_UNREACHABLE = "WORKFLOW_UNREACHABLE"
WORKFLOW_REFERENCE_INVALID = "WORKFLOW_REFERENCE_INVALID"
WORKFLOW_REFERENCE_UNKNOWN = "WORKFLOW_REFERENCE_UNKNOWN"
WORKFLOW_REFERENCE_NOT_UPSTREAM = "WORKFLOW_REFERENCE_NOT_UPSTREAM"
WORKFLOW_TEMPLATE_INVALID = "WORKFLOW_TEMPLATE_INVALID"
WORKFLOW_CEL_INVALID = "WORKFLOW_CEL_INVALID"
WORKFLOW_TOOL_UNAVAILABLE = "WORKFLOW_TOOL_UNAVAILABLE"
WORKFLOW_KNOWLEDGE_BASE_UNKNOWN = "WORKFLOW_KNOWLEDGE_BASE_UNKNOWN"
WORKFLOW_APPROVER_INVALID = "WORKFLOW_APPROVER_INVALID"
WORKFLOW_VERSION_HASH_MISMATCH = "WORKFLOW_VERSION_HASH_MISMATCH"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_CEL_REFERENCE_RE = re.compile(r"\b(inputs|outputs)\.([A-Za-z_][A-Za-z0-9_]*)")

_LIMIT_DEFAULTS: dict[str, int] = {
    "max_steps": 50,
    "max_duration_sec": 600,
    "max_output_bytes": 1_048_576,
    "timeout_per_step_sec": 120,
}
_OUTPUT_DEFAULT: dict[str, str] = {"format": "json", "destination": "user"}

# A CEL probe classifies failures: these mean the expression itself is invalid,
# these mean the sandbox could not run (fail closed instead of guessing), and
# anything else (for example a missing key in the empty probe context) means the
# expression parsed and its value is resolved from real inputs at run time.
_CEL_INVALID_CODES = frozenset(
    {
        "CEL_SYNTAX_ERROR",
        "CEL_EXPRESSION_TOO_LONG",
        "CEL_AST_DEPTH_EXCEEDED",
        "CEL_COST_EXCEEDED",
        "CEL_OUTPUT_NOT_JSON",
        "CEL_INVALID_EXPRESSION",
    }
)
_CEL_UNAVAILABLE_CODES = frozenset(
    {
        "CEL_SANDBOX_ERROR",
        "CEL_WORKER_STARTUP_TIMEOUT",
        "CEL_TIMEOUT",
        "CEL_MEMORY_LIMIT_EXCEEDED",
        "CEL_MEMORY_LIMIT_UNAVAILABLE",
        "CEL_INVALID_CONTEXT",
    }
)

_TEMPLATE_FIELD_PATHS: Mapping[str, str] = {
    "tool": "parameters",
    "llm": "prompt",
    "transform": "input",
    "approval": "approval_message",
}


class WorkflowCompileError(ValueError):
    """A workflow definition that must never be persisted."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    """A resolver verdict about one tenant-scoped reference.

    ``code`` names the failure with a stable string; a code that matches an
    :class:`~octop.infra.errors.ErrorCode` member surfaces with that member.
    """

    ok: bool
    code: str = ""
    message: str = ""

    @classmethod
    def allowed(cls) -> SemanticDecision:
        return cls(True)

    @classmethod
    def refused(cls, code: str, message: str) -> SemanticDecision:
        return cls(False, code, message)


@runtime_checkable
class WorkflowSemanticResolver(Protocol):
    """Resolve tenant-scoped references that the definition cannot prove itself.

    Implementations run inside the caller's WorkBuddy transaction, so the tenant
    scope is already applied; they must never disclose another tenant's object.
    Returning ``None`` means "resolved"; a refusal is a :class:`SemanticDecision`
    with ``ok=False``.  Raising is also allowed: the caller then fails closed.
    """

    def check_tool(
        self, tool_name: str, parameters: Mapping[str, Any]
    ) -> SemanticDecision | None: ...

    def check_model(self, model: str | None) -> SemanticDecision | None: ...

    def check_knowledge_base(self, knowledge_base_id: str) -> SemanticDecision | None: ...

    def check_approver(self, user_id: str) -> SemanticDecision | None: ...


@dataclass(frozen=True, slots=True)
class CompiledEdge:
    """One effective edge; an approval ``target_node_id`` becomes an untagged edge."""

    from_node_id: str
    to_node_id: str
    when: str | None = None
    implicit: bool = False

    @property
    def branched(self) -> bool:
        return self.when is not None


@dataclass(frozen=True, slots=True)
class CompiledNode:
    node_id: str
    node_type: str
    name: str
    config: Mapping[str, Any]
    save_as: str | None
    retry: Mapping[str, Any] | None
    upstream_node_ids: frozenset[str]
    incoming: tuple[CompiledEdge, ...]
    outgoing: tuple[CompiledEdge, ...]


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """The canonical form of one definition; identity is ``definition_sha256``."""

    schema_version: int
    compiler_version: str
    definition: Mapping[str, Any]
    definition_sha256: str
    entry_node_id: str
    nodes: tuple[CompiledNode, ...]
    edges: tuple[CompiledEdge, ...]
    topological_order: tuple[str, ...]
    exit_node_ids: tuple[str, ...]
    node_by_id: Mapping[str, CompiledNode]
    save_as_by_node: Mapping[str, str]
    output_key_by_node: Mapping[str, str]
    output_key_to_node: Mapping[str, str]
    semantic_checks: str
    cel_evidence: Mapping[str, Mapping[str, Any]]

    def node(self, node_id: str) -> CompiledNode:
        return self.node_by_id[node_id]

    def output_key(self, node_id: str) -> str:
        """Runtime output key for a node: ``save_as`` or the node id."""
        return self.output_key_by_node[node_id]


# --------------------------------------------------------------------------- #
# schema loading
# --------------------------------------------------------------------------- #


def _schema_candidates() -> list[Path]:
    candidates: list[Path] = []
    override = (os.environ.get(WORKFLOW_SCHEMA_ENV) or "").strip()
    if override:
        candidates.append(Path(override))
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "contracts" / WORKFLOW_SCHEMA_FILENAME)
    return candidates


def schema_path() -> Path | None:
    """First existing local schema file; ``None`` when the checkout lacks one."""
    for candidate in _schema_candidates():
        if candidate.is_file():
            return candidate
    return None


def _assert_local_references(node: Any, *, pointer: str = "#") -> None:
    """Reject every non-local ``$ref``: schemas are resolved locally only."""
    if isinstance(node, Mapping):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            raise WorkflowCompileError(
                WORKFLOW_DEPENDENCY_UNAVAILABLE,
                f"workflow schema uses the non-local reference {reference!r}",
                path=pointer,
            )
        for key, value in node.items():
            _assert_local_references(value, pointer=f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_local_references(value, pointer=f"{pointer}/{index}")


@lru_cache(maxsize=1)
def workflow_schema() -> Mapping[str, Any]:
    """The checked-in Draft 7 schema; treated as read-only by every caller."""
    path = schema_path()
    if path is None:
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE,
            f"workflow schema {WORKFLOW_SCHEMA_FILENAME} is not available locally",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE, "workflow schema could not be read"
        ) from exc
    if not isinstance(raw, dict):
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE, "workflow schema must be a JSON object"
        )
    _assert_local_references(raw)
    try:
        Draft7Validator.check_schema(raw)
    except SchemaError as exc:
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE, f"workflow schema is malformed: {exc.message}"
        ) from exc
    return raw


@lru_cache(maxsize=1)
def workflow_format_checker() -> FormatChecker:
    """Draft 7 format checks; ``uuid`` must be registered for this to be meaningful."""
    checker = FormatChecker()
    if "uuid" not in checker.checkers:
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE, "jsonschema format checker lacks uuid support"
        )
    return checker


@lru_cache(maxsize=1)
def workflow_validator() -> Draft7Validator:
    return Draft7Validator(workflow_schema(), format_checker=workflow_format_checker())


def reset_schema_cache() -> None:
    """Drop the cached schema (tests and deployments that switch the override)."""
    workflow_schema.cache_clear()
    workflow_format_checker.cache_clear()
    workflow_validator.cache_clear()


# --------------------------------------------------------------------------- #
# canonical form
# --------------------------------------------------------------------------- #


def _strict_json_copy(value: Any, *, path: str = "definition") -> Any:
    """Copy a JSON tree, rejecting values that do not survive canonical JSON."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowCompileError(
                WORKFLOW_SCHEMA_INVALID, "definition contains a non-finite number", path=path
            )
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowCompileError(
                    WORKFLOW_SCHEMA_INVALID, "definition object keys must be strings", path=path
                )
            copied[key] = _strict_json_copy(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        return [
            _strict_json_copy(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise WorkflowCompileError(
        WORKFLOW_SCHEMA_INVALID,
        f"definition contains a non-JSON {type(value).__name__} value",
        path=path,
    )


def canonical_definition_json(definition: Any) -> str:
    """Deterministic canonical JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(
        definition,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def definition_sha256(definition: Any) -> str:
    """SHA-256 over the canonical JSON of a definition."""
    return hashlib.sha256(canonical_definition_json(definition).encode("utf-8")).hexdigest()


def normalize_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Apply every schema default and a canonical node/edge order.

    Idempotent: ``normalize_definition(normalize_definition(d)) ==
    normalize_definition(d)``.
    """
    normalized = _strict_json_copy(definition)
    inputs = normalized.setdefault("inputs", {})
    if isinstance(inputs, Mapping):
        for declaration in inputs.values():
            if isinstance(declaration, Mapping):
                declaration.setdefault("required", False)

    limits = normalized.get("limits")
    if not isinstance(limits, Mapping):
        limits = {}
        normalized["limits"] = limits
    for key, value in _LIMIT_DEFAULTS.items():
        limits.setdefault(key, value)

    output = normalized.get("output")
    if not isinstance(output, Mapping):
        normalized["output"] = dict(_OUTPUT_DEFAULT)

    nodes = normalized.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            retry = node.get("retry")
            if isinstance(retry, Mapping):
                retry.setdefault("max_attempts", 1)
                retry.setdefault("backoff_sec", 5)
            config = node.get("config")
            if node.get("type") == "approval" and isinstance(config, Mapping):
                config.setdefault("timeout_hours", 24)
        normalized["nodes"] = sorted(nodes, key=lambda item: str(item.get("id", "")))

    edges = normalized.get("edges")
    if isinstance(edges, list):
        normalized["edges"] = sorted(
            edges,
            key=lambda item: (
                str(item.get("from", "")),
                str(item.get("to", "")),
                str(item.get("when") or ""),
            ),
        )
    return normalized


# --------------------------------------------------------------------------- #
# reference and template rules
# --------------------------------------------------------------------------- #


def _require_identifier(value: Any, *, kind: str, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise WorkflowCompileError(
            WORKFLOW_REFERENCE_INVALID, f"{kind} is not a valid workflow identifier", path=path
        )
    return value


def parse_reference(reference: str, *, path: str = "") -> tuple[str, str]:
    """Parse a path-safe reference: ``inputs.<name>`` or ``nodes.<node_id>.output``.

    Template references name the producing node; CEL ``outputs`` references name
    the ``save_as`` value (see :func:`iter_cel_references`).
    """
    text = reference.strip()
    if not text or len(text) > MAX_REFERENCE_LENGTH:
        raise WorkflowCompileError(
            WORKFLOW_REFERENCE_INVALID, "workflow reference is empty or too long", path=path
        )
    parts = text.split(".")
    if len(parts) == 2 and parts[0] == "inputs":
        return "input", _require_identifier(parts[1], kind="input name", path=path)
    if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "output":
        return "node", _require_identifier(parts[1], kind="node id", path=path)
    raise WorkflowCompileError(
        WORKFLOW_REFERENCE_INVALID,
        "workflow references must be inputs.<name> or nodes.<node_id>.output",
        path=path,
    )


def iter_template_references(value: Any, *, path: str) -> list[tuple[str, str]]:
    """Every reference in a restricted one-pass template string tree."""
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        body = value
        matches = list(_TEMPLATE_RE.finditer(body))
        remainder = _TEMPLATE_RE.sub("", body)
        if "{{" in remainder or "}}" in remainder:
            raise WorkflowCompileError(
                WORKFLOW_TEMPLATE_INVALID, "template has an unterminated placeholder", path=path
            )
        if len(matches) > MAX_TEMPLATE_PLACEHOLDERS:
            raise WorkflowCompileError(
                WORKFLOW_TEMPLATE_INVALID,
                f"template has more than {MAX_TEMPLATE_PLACEHOLDERS} placeholders",
                path=path,
            )
        for match in matches:
            parsed = parse_reference(match.group(1), path=path)
            found.append((parsed[0], parsed[1]))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(iter_template_references(item, path=f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            found.extend(iter_template_references(item, path=f"{path}[{index}]"))
    return found


def iter_cel_references(expression: str) -> list[tuple[str, str]]:
    """Conservative static scan of ``inputs.<name>`` / ``outputs.<save_as>`` in CEL."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _CEL_REFERENCE_RE.finditer(expression):
        kind = "input" if match.group(1) == "inputs" else "output"
        name = match.group(2)
        key = (kind, name)
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def _probe_cel(expression: str, *, path: str) -> dict[str, Any]:
    """Parse one CEL expression through the bounded sandbox and return evidence."""
    try:
        evaluate_cel(expression, {"inputs": {}, "outputs": {}})
    except CELSandboxError as exc:
        if exc.code in _CEL_INVALID_CODES:
            raise WorkflowCompileError(
                WORKFLOW_CEL_INVALID, f"CEL expression is invalid: {exc.message}", path=path
            ) from exc
        if exc.code in _CEL_UNAVAILABLE_CODES:
            raise WorkflowCompileError(
                WORKFLOW_DEPENDENCY_UNAVAILABLE,
                f"CEL sandbox unavailable: {exc.message}",
                path=path,
            ) from exc
        # Evaluation-only failure against the empty probe context: syntax is fine.
    return {"runner": "InterpretedRunner", "probe": "empty_context"}


# --------------------------------------------------------------------------- #
# compilation
# --------------------------------------------------------------------------- #


class _Compiler:
    """State for one compile pass; every check fails closed with a coded error."""

    def __init__(self, definition: Mapping[str, Any]) -> None:
        self.normalized = normalize_definition(definition)
        self.nodes: list[Mapping[str, Any]] = list(self.normalized.get("nodes") or [])
        self.edge_specs: list[Mapping[str, Any]] = list(self.normalized.get("edges") or [])
        self.input_names: tuple[str, ...] = tuple(
            sorted((self.normalized.get("inputs") or {}).keys())
        )
        self.node_ids: tuple[str, ...] = tuple(str(node["id"]) for node in self.nodes)
        self.node_by_id: dict[str, Mapping[str, Any]] = {
            str(node["id"]): node for node in self.nodes
        }
        self.save_as_by_node: dict[str, str] = {}
        self.output_key_to_node: dict[str, str] = {}
        self.effective_edges: list[CompiledEdge] = []
        self.incoming: dict[str, list[CompiledEdge]] = {node_id: [] for node_id in self.node_ids}
        self.outgoing: dict[str, list[CompiledEdge]] = {node_id: [] for node_id in self.node_ids}
        self.entry_node_id = ""
        self.topological_order: tuple[str, ...] = ()
        self.ancestors: dict[str, frozenset[str]] = {}
        self.cel_evidence: dict[str, dict[str, Any]] = {}

    # -- graph ------------------------------------------------------------- #

    def check_node_identity(self) -> None:
        seen: dict[str, str] = {}
        for node in self.nodes:
            node_id = str(node["id"])
            if node_id in seen:
                raise WorkflowCompileError(
                    WORKFLOW_NODE_DUPLICATE_ID,
                    f"node id {node_id!r} is used more than once",
                    path=f"nodes.{node_id}.id",
                )
            seen[node_id] = node_id
            save_as = node.get("save_as")
            output_key = str(save_as) if save_as is not None else node_id
            producer = self.output_key_to_node.get(output_key)
            if producer is not None:
                raise WorkflowCompileError(
                    WORKFLOW_SAVE_AS_DUPLICATE,
                    f"output key {output_key!r} is produced by both {producer!r} and {node_id!r}",
                    path=f"nodes.{node_id}.save_as",
                )
            self.output_key_to_node[output_key] = node_id
            if save_as is not None:
                self.save_as_by_node[node_id] = str(save_as)

    def _approval_target(self, node: Mapping[str, Any]) -> str | None:
        config = node.get("config") or {}
        target = config.get("target_node_id")
        return str(target) if target else None

    def check_edges(self) -> None:
        seen_pairs: set[tuple[str, str]] = set()
        for index, edge in enumerate(self.edge_specs):
            source = str(edge["from"])
            target = str(edge["to"])
            when = edge.get("when")
            path = f"edges[{index}]"
            if source not in self.node_by_id:
                raise WorkflowCompileError(
                    WORKFLOW_EDGE_UNKNOWN_NODE, f"edge source {source!r} is not a node", path=path
                )
            if target not in self.node_by_id:
                raise WorkflowCompileError(
                    WORKFLOW_EDGE_UNKNOWN_NODE, f"edge target {target!r} is not a node", path=path
                )
            if source == target:
                raise WorkflowCompileError(
                    WORKFLOW_EDGE_SELF_LOOP, f"node {source!r} cannot point at itself", path=path
                )
            if (source, target) in seen_pairs:
                raise WorkflowCompileError(
                    WORKFLOW_EDGE_DUPLICATE,
                    f"edge {source!r} -> {target!r} is duplicated",
                    path=path,
                )
            seen_pairs.add((source, target))
            node_type = str(self.node_by_id[source].get("type"))
            if when is None and node_type == "condition":
                raise WorkflowCompileError(
                    WORKFLOW_CONDITION_EDGES,
                    f"condition node {source!r} needs both a true and a false edge",
                    path=path,
                )
            if when is not None and node_type != "condition":
                raise WorkflowCompileError(
                    WORKFLOW_EDGE_UNEXPECTED_WHEN,
                    f"only condition nodes may branch on when=; {source!r} is a {node_type} node",
                    path=path,
                )
            self.effective_edges.append(CompiledEdge(source, target, when))

        for node in self.nodes:
            node_id = str(node["id"])
            node_type = str(node.get("type"))
            explicit = [edge for edge in self.effective_edges if edge.from_node_id == node_id]
            if node_type == "condition":
                whens = sorted(edge.when for edge in explicit)
                if whens != ["false", "true"]:
                    raise WorkflowCompileError(
                        WORKFLOW_CONDITION_EDGES,
                        f"condition node {node_id!r} needs exactly one true and one false edge",
                        path=f"nodes.{node_id}.config.expression",
                    )
                continue
            if len(explicit) > 1:
                raise WorkflowCompileError(
                    WORKFLOW_FAN_OUT,
                    f"node {node_id!r} has {len(explicit)} outgoing edges; only condition nodes branch",
                    path=f"nodes.{node_id}",
                )
            target = self._approval_target(node)
            if target is None:
                continue
            if node_type != "approval":
                raise WorkflowCompileError(
                    WORKFLOW_APPROVAL_TARGET,
                    f"node {node_id!r} is not an approval node and cannot declare target_node_id",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
            if explicit:
                raise WorkflowCompileError(
                    WORKFLOW_APPROVAL_EDGES,
                    f"approval node {node_id!r} declares target_node_id and an outgoing edge",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
            if target not in self.node_by_id:
                raise WorkflowCompileError(
                    WORKFLOW_APPROVAL_TARGET,
                    f"approval target {target!r} is not a node",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
            self.effective_edges.append(CompiledEdge(node_id, target, when=None, implicit=True))

        for edge in self.effective_edges:
            self.outgoing[edge.from_node_id].append(edge)
            self.incoming[edge.to_node_id].append(edge)

    def check_topology(self) -> None:
        entries = sorted(node_id for node_id in self.node_ids if not self.incoming[node_id])
        if len(entries) != 1:
            raise WorkflowCompileError(
                WORKFLOW_ENTRY_COUNT,
                f"workflow needs exactly one entry node; found {len(entries)}",
                details={"entry_candidates": entries},
            )
        self.entry_node_id = entries[0]

        indegree = {node_id: len(self.incoming[node_id]) for node_id in self.node_ids}
        ready = list(entries)
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            node_id = heapq.heappop(ready)
            order.append(node_id)
            for edge in sorted(
                self.outgoing[node_id],
                key=lambda item: (item.to_node_id, item.when or ""),
            ):
                indegree[edge.to_node_id] -= 1
                if indegree[edge.to_node_id] == 0:
                    heapq.heappush(ready, edge.to_node_id)

        if len(order) != len(self.node_ids):
            blocked = sorted(set(self.node_ids) - set(order))
            raise WorkflowCompileError(
                WORKFLOW_CYCLE,
                "workflow graph contains a cycle",
                details={"cycle_nodes": blocked},
            )
        self.topological_order = tuple(order)

        reached = {self.entry_node_id}
        pending = [self.entry_node_id]
        while pending:
            current = pending.pop()
            for edge in self.outgoing[current]:
                if edge.to_node_id not in reached:
                    reached.add(edge.to_node_id)
                    pending.append(edge.to_node_id)
        unreachable = sorted(set(self.node_ids) - reached)
        if unreachable:
            raise WorkflowCompileError(
                WORKFLOW_UNREACHABLE,
                "workflow has nodes that are not reachable from the entry node",
                details={"unreachable_nodes": unreachable},
            )

        ancestors: dict[str, frozenset[str]] = {}
        for node_id in self.topological_order:
            accumulated: set[str] = set()
            for edge in self.incoming[node_id]:
                accumulated.add(edge.from_node_id)
                accumulated |= ancestors[edge.from_node_id]
            ancestors[node_id] = frozenset(accumulated)
        self.ancestors = ancestors

        for node in self.nodes:
            node_id = str(node["id"])
            target = self._approval_target(node)
            if target is None:
                continue
            if target == node_id or target in self.ancestors[node_id]:
                raise WorkflowCompileError(
                    WORKFLOW_APPROVAL_TARGET,
                    f"approval target {target!r} is not downstream of {node_id!r}",
                    path=f"nodes.{node_id}.config.target_node_id",
                )

    # -- references -------------------------------------------------------- #

    def _check_reference(
        self, kind: str, name: str, *, node_id: str, path: str, scope: str
    ) -> None:
        if kind == "input":
            if name not in self.input_names:
                raise WorkflowCompileError(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference inputs.{name} is not a declared input",
                    path=path,
                    details={"scope": scope},
                )
            return
        if kind == "node":
            producer = self.node_by_id.get(name)
            if producer is None:
                raise WorkflowCompileError(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference nodes.{name}.output is not a node in this workflow",
                    path=path,
                    details={"scope": scope},
                )
            owner = name
        else:
            owner = self.output_key_to_node.get(name)
            if owner is None:
                raise WorkflowCompileError(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference outputs.{name} has no unique producer",
                    path=path,
                    details={"scope": scope},
                )
        if owner not in self.ancestors[node_id]:
            raise WorkflowCompileError(
                WORKFLOW_REFERENCE_NOT_UPSTREAM,
                f"reference to {name!r} is not upstream of node {node_id!r}",
                path=path,
                details={"scope": scope, "producer": owner},
            )

    def check_references(self) -> None:
        for node in self.nodes:
            node_id = str(node["id"])
            node_type = str(node.get("type"))
            config = node.get("config") or {}
            field = _TEMPLATE_FIELD_PATHS.get(node_type)
            if field is not None and field in config:
                for kind, name in iter_template_references(
                    config[field], path=f"nodes.{node_id}.config.{field}"
                ):
                    self._check_reference(
                        kind,
                        name,
                        node_id=node_id,
                        path=f"nodes.{node_id}.config.{field}",
                        scope="template",
                    )
            if node_type in {"condition", "transform"}:
                expression = str(config.get("expression") or "")
                expression_path = f"nodes.{node_id}.config.expression"
                self.cel_evidence[expression_path] = _probe_cel(expression, path=expression_path)
                for kind, name in iter_cel_references(expression):
                    self._check_reference(
                        kind, name, node_id=node_id, path=expression_path, scope="cel"
                    )

    # -- semantics --------------------------------------------------------- #

    def check_semantics(
        self,
        resolver: WorkflowSemanticResolver | None,
        *,
        require_semantic_resolution: bool,
    ) -> str:
        needed = any(str(node.get("type")) in {"tool", "llm", "approval"} for node in self.nodes)
        if not needed:
            return "not_required"
        if resolver is None:
            if require_semantic_resolution:
                raise WorkflowCompileError(
                    WORKFLOW_DEPENDENCY_UNAVAILABLE,
                    "workflow definition needs semantic resolution but no resolver is configured",
                )
            return "skipped"
        for node in self.nodes:
            node_id = str(node["id"])
            node_type = str(node.get("type"))
            config = node.get("config") or {}
            if node_type == "tool":
                self._apply_decision(
                    resolver.check_tool(
                        str(config.get("tool_name")), config.get("parameters") or {}
                    ),
                    fallback_code=WORKFLOW_TOOL_UNAVAILABLE,
                    fallback_message=f"tool {config.get('tool_name')!r} is not available",
                    path=f"nodes.{node_id}.config.tool_name",
                )
            elif node_type == "llm":
                self._apply_decision(
                    resolver.check_model(config.get("model")),
                    fallback_code=WORKFLOW_MODEL_NOT_CONFIGURED,
                    fallback_message="no tenant model is configured for this llm node",
                    path=f"nodes.{node_id}.config.model",
                )
                for index, knowledge_base_id in enumerate(config.get("knowledge_base_ids") or []):
                    self._apply_decision(
                        resolver.check_knowledge_base(str(knowledge_base_id)),
                        fallback_code=WORKFLOW_KNOWLEDGE_BASE_UNKNOWN,
                        fallback_message="knowledge base is not visible in this tenant",
                        path=f"nodes.{node_id}.config.knowledge_base_ids[{index}]",
                    )
            elif node_type == "approval":
                for index, approver in enumerate(config.get("approver_user_ids") or []):
                    self._apply_decision(
                        resolver.check_approver(str(approver)),
                        fallback_code=WORKFLOW_APPROVER_INVALID,
                        fallback_message="approver is not an active member of this tenant",
                        path=f"nodes.{node_id}.config.approver_user_ids[{index}]",
                    )
        return "passed"

    @staticmethod
    def _apply_decision(
        decision: SemanticDecision | None,
        *,
        fallback_code: str,
        fallback_message: str,
        path: str,
    ) -> None:
        if decision is None or decision.ok:
            return
        raise WorkflowCompileError(
            decision.code or fallback_code,
            decision.message or fallback_message,
            path=path,
        )

    # -- result ------------------------------------------------------------ #

    def build(self, *, semantic_checks: str) -> CompiledWorkflow:
        compiled_nodes: list[CompiledNode] = []
        for node in self.nodes:
            node_id = str(node["id"])
            compiled_nodes.append(
                CompiledNode(
                    node_id=node_id,
                    node_type=str(node["type"]),
                    name=str(node["name"]),
                    config=dict(node.get("config") or {}),
                    save_as=self.save_as_by_node.get(node_id),
                    retry=dict(node["retry"]) if node.get("retry") else None,
                    upstream_node_ids=self.ancestors[node_id],
                    incoming=tuple(
                        sorted(
                            self.incoming[node_id],
                            key=lambda item: (item.from_node_id, item.when or ""),
                        )
                    ),
                    outgoing=tuple(
                        sorted(
                            self.outgoing[node_id],
                            key=lambda item: (item.to_node_id, item.when or ""),
                        )
                    ),
                )
            )
        edges = tuple(
            sorted(self.effective_edges, key=lambda item: (item.from_node_id, item.to_node_id))
        )
        exit_nodes = tuple(
            sorted(node_id for node_id in self.node_ids if not self.outgoing[node_id])
        )
        return CompiledWorkflow(
            schema_version=int(self.normalized["schema_version"]),
            compiler_version=WORKFLOW_COMPILER_VERSION,
            definition=self.normalized,
            definition_sha256=definition_sha256(self.normalized),
            entry_node_id=self.entry_node_id,
            nodes=tuple(compiled_nodes),
            edges=edges,
            topological_order=self.topological_order,
            exit_node_ids=exit_nodes,
            node_by_id={node.node_id: node for node in compiled_nodes},
            save_as_by_node=dict(self.save_as_by_node),
            output_key_by_node={
                node_id: (self.save_as_by_node.get(node_id) or node_id) for node_id in self.node_ids
            },
            output_key_to_node=dict(self.output_key_to_node),
            semantic_checks=semantic_checks,
            cel_evidence={key: dict(value) for key, value in self.cel_evidence.items()},
        )


def _schema_errors(definition: Any) -> list[ValidationError]:
    errors = list(workflow_validator().iter_errors(definition))
    return sorted(errors, key=lambda error: (list(error.absolute_path), error.message))


def compile_workflow_definition(
    definition: Any,
    *,
    resolver: WorkflowSemanticResolver | None = None,
    require_semantic_resolution: bool = False,
) -> CompiledWorkflow:
    """Validate, normalize and compile one workflow definition.

    ``require_semantic_resolution`` makes missing tool/model/knowledge-base or
    approver resolution a hard failure instead of a skipped check; persistence
    callers must set it so an unproven reference is never stored.
    """
    if not isinstance(definition, Mapping):
        raise WorkflowCompileError(
            WORKFLOW_SCHEMA_INVALID, "workflow definition must be a JSON object"
        )
    errors = _schema_errors(definition)
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path)
        raise WorkflowCompileError(
            WORKFLOW_SCHEMA_INVALID,
            first.message,
            path=path,
            details={
                "errors": [
                    {
                        "path": ".".join(str(part) for part in error.absolute_path),
                        "message": error.message,
                    }
                    for error in errors[:20]
                ]
            },
        )
    compiler = _Compiler(definition)
    compiler.check_node_identity()
    compiler.check_edges()
    compiler.check_topology()
    compiler.check_references()
    semantic_checks = compiler.check_semantics(
        resolver, require_semantic_resolution=require_semantic_resolution
    )
    return compiler.build(semantic_checks=semantic_checks)


def verify_definition_hash(definition: Any, expected_sha256: str) -> None:
    """Fail when a stored definition no longer matches its recorded hash."""
    actual = definition_sha256(definition)
    if actual != str(expected_sha256):
        raise WorkflowCompileError(
            WORKFLOW_VERSION_HASH_MISMATCH,
            "stored workflow definition does not match its recorded hash",
            details={"expected": str(expected_sha256), "actual": actual},
        )


def compile_stored_definition(
    definition: Any,
    expected_sha256: str,
    *,
    resolver: WorkflowSemanticResolver | None = None,
    require_semantic_resolution: bool = False,
) -> CompiledWorkflow:
    """Re-verify a persisted immutable version, then compile it for execution."""
    if not isinstance(definition, Mapping):
        raise WorkflowCompileError(
            WORKFLOW_VERSION_HASH_MISMATCH, "stored workflow definition must be a JSON object"
        )
    verify_definition_hash(definition, expected_sha256)
    return compile_workflow_definition(
        definition,
        resolver=resolver,
        require_semantic_resolution=require_semantic_resolution,
    )


__all__ = [
    "ACTIVATABLE_VERSION_ORIGINS",
    "CANDIDATE_VERSION_ORIGINS",
    "CompiledEdge",
    "CompiledNode",
    "CompiledWorkflow",
    "MAX_REFERENCE_LENGTH",
    "MAX_TEMPLATE_PLACEHOLDERS",
    "NODE_TYPES",
    "SemanticDecision",
    "VERSION_ORIGINS",
    "WORKFLOW_COMPILER_VERSION",
    "WORKFLOW_SCHEMA_ENV",
    "WORKFLOW_SCHEMA_FILENAME",
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowCompileError",
    "WorkflowSemanticResolver",
    "canonical_definition_json",
    "compile_stored_definition",
    "compile_workflow_definition",
    "definition_sha256",
    "iter_cel_references",
    "iter_template_references",
    "normalize_definition",
    "parse_reference",
    "reset_schema_cache",
    "schema_path",
    "verify_definition_hash",
    "workflow_schema",
    "workflow_validator",
]
