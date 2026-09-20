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

from octop.infra.errors import ErrorCode
from octop.infra.workbuddy.cel_sandbox import CELSandboxError, evaluate_cel

WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_COMPILER_VERSION = "workbuddy-workflow-compiler/1"
WORKFLOW_SCHEMA_FILENAME = "workflow-v1.schema.json"
WORKFLOW_SCHEMA_ENV = "WORKBUDDY_WORKFLOW_SCHEMA_PATH"

MAX_TEMPLATE_PLACEHOLDERS = 32
MAX_REFERENCE_LENGTH = 200
#: Refusals that describe the definition are aggregated; the cap keeps a hostile
#: draft from turning one 422 into an unbounded response body.
MAX_DIAGNOSTICS = 20
#: Prefix of every diagnostic hint key: clients localize them, this module never
#: carries prose for a repair hint.
DIAGNOSTIC_HINT_PREFIX = "workflowDiagnostics"

NODE_TYPES = frozenset(
    {"tool", "llm", "condition", "approval", "ask", "transform", "input", "knowledge", "output"}
)
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
WORKFLOW_ASK_FIELDS_DUPLICATE = "WORKFLOW_ASK_FIELDS_DUPLICATE"
WORKFLOW_INPUT_NODE_UNKNOWN = "WORKFLOW_INPUT_NODE_UNKNOWN"
WORKFLOW_OUTPUT_NOT_TERMINAL = "WORKFLOW_OUTPUT_NOT_TERMINAL"
WORKFLOW_OUTPUT_DUPLICATE = "WORKFLOW_OUTPUT_DUPLICATE"
WORKFLOW_VERSION_HASH_MISMATCH = "WORKFLOW_VERSION_HASH_MISMATCH"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

#: The reference grammar :func:`parse_reference` accepts, keyed by reference kind.
#: A template placeholder carries one of these; node metadata publishes the same
#: map instead of restating the grammar.
REFERENCE_SYNTAX: Mapping[str, str] = {
    "input": "inputs.<name>",
    "node": "nodes.<node_id>.output",
}
#: CEL namespaces, each mapped to the reference kind :func:`iter_cel_references`
#: reports for it and to the operand grammar that namespace follows.
CEL_REFERENCE_NAMESPACES: Mapping[str, tuple[str, str]] = {
    "inputs": ("input", "inputs.<name>"),
    "outputs": ("output", "outputs.<save_as>"),
}
_CEL_REFERENCE_RE = re.compile(
    rf"\b({'|'.join(CEL_REFERENCE_NAMESPACES)})\.([A-Za-z_][A-Za-z0-9_]*)"
)

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

#: A refusal that names an unreachable dependency rather than a defect of the
#: definition; it must keep its own code at the API boundary.
_FATAL_REFUSAL_CODES = frozenset({WORKFLOW_DEPENDENCY_UNAVAILABLE})

#: The one config field of each node type where ``{{ … }}`` placeholders are read;
#: every other field stays opaque to the template scanner.
TEMPLATE_FIELD_PATHS: Mapping[str, str] = {
    "tool": "parameters",
    "llm": "prompt",
    "transform": "input",
    "approval": "approval_message",
    "ask": "prompt",
    "knowledge": "query",
    "output": "value",
}

#: The field types an ask node's form may declare, and the JSON value each one
#: stores.  The schema enumerates the same set; a validator that accepts a value
#: for a type the schema does not define would let a caller store something no
#: step can read back the same way twice.
ASK_FIELD_TYPES: frozenset[str] = frozenset(
    {"string", "text", "integer", "number", "boolean", "date", "select"}
)


_NODE_PATH_RE = re.compile(r"^nodes\.([^.[\]]+)")


def _node_id_from_path(path: str) -> str | None:
    """The node a diagnostic points at (``nodes.<id>.…``), when its path names one."""
    match = _NODE_PATH_RE.match(path)
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class WorkflowDiagnostic:
    """One reason a definition was refused, with the location that carries it.

    ``node_id`` is the node the path addresses and ``hint_key`` is the i18n key
    for its repair hint — always a key, never prose, so a client localizes the
    hint instead of guessing at the code.
    """

    code: str
    message: str
    path: str = ""
    node_id: str | None = None
    hint_key: str | None = None

    def __post_init__(self) -> None:
        if self.hint_key is None:
            object.__setattr__(self, "hint_key", f"{DIAGNOSTIC_HINT_PREFIX}.{self.code}")
        if self.node_id is None:
            object.__setattr__(self, "node_id", _node_id_from_path(self.path))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            payload["path"] = self.path
        if self.node_id:
            payload["node_id"] = self.node_id
        if self.hint_key:
            payload["hint_key"] = self.hint_key
        return payload


class WorkflowCompileError(ValueError):
    """A workflow definition that must never be persisted.

    One refusal may carry several diagnostics: a check phase proves everything
    it can before raising, so a caller sees every defect of that phase at once
    instead of one per round trip.  ``code``/``message``/``path``/``details``
    always describe the primary diagnostic — the first in the stable
    ``(path, code)`` order — so a definition with a single defect refuses
    exactly as it did before this class aggregated.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        details: Mapping[str, Any] | None = None,
        diagnostics: Sequence[WorkflowDiagnostic] | None = None,
    ) -> None:
        collected = tuple(diagnostics or ())
        if not collected:
            collected = (WorkflowDiagnostic(code=code, message=message, path=path),)
        ordered = sorted(collected, key=lambda item: (item.path, item.code))
        payload = dict(details or {})
        if len(ordered) > MAX_DIAGNOSTICS:
            payload["diagnostics_truncated"] = len(ordered)
            ordered = ordered[:MAX_DIAGNOSTICS]
        primary = ordered[0]
        super().__init__(primary.message)
        self.code = primary.code
        self.message = primary.message
        self.path = primary.path
        self.details = payload
        self.diagnostics: tuple[WorkflowDiagnostic, ...] = tuple(ordered)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        payload["diagnostics"] = [diagnostic.to_dict() for diagnostic in self.diagnostics]
        return payload


class _DiagnosticCollector:
    """Every refusal one check phase proved, raised together.

    Phases stay sequential — each one builds the state the next one proves
    against — but inside a phase every independent defect is collected.  An
    infrastructure refusal (:data:`WORKFLOW_DEPENDENCY_UNAVAILABLE`) is not a
    defect of the definition at all and aborts at once, so it keeps its own code
    at the API boundary instead of being sorted behind a syntax error.
    """

    def __init__(self) -> None:
        self._entries: list[tuple[WorkflowDiagnostic, dict[str, Any]]] = []

    def __bool__(self) -> bool:
        return bool(self._entries)

    def add(
        self, diagnostic: WorkflowDiagnostic, *, details: Mapping[str, Any] | None = None
    ) -> None:
        if diagnostic.code in _FATAL_REFUSAL_CODES:
            raise WorkflowCompileError(
                diagnostic.code, diagnostic.message, path=diagnostic.path, details=details
            )
        self._entries.append((diagnostic, dict(details or {})))

    def refuse(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.add(WorkflowDiagnostic(code=code, message=message, path=path), details=details)

    def absorb(self, exc: WorkflowCompileError) -> None:
        """Fold a nested refusal in; the phase keeps collecting."""
        for diagnostic in exc.diagnostics:
            self.add(diagnostic, details=exc.details)

    def raise_if_any(self, stage: str) -> None:
        """Raise one aggregated refusal, or return when the phase proved nothing."""
        if not self._entries:
            return
        ordered = sorted(self._entries, key=lambda entry: (entry[0].path, entry[0].code))
        primary, primary_details = ordered[0]
        details = dict(primary_details)
        details["stage"] = stage
        raise WorkflowCompileError(
            primary.code,
            primary.message,
            path=primary.path,
            details=details,
            diagnostics=tuple(diagnostic for diagnostic, _ in ordered),
        )


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


def _reject_non_local_references(node: Any, *, path: str) -> None:
    """Every ``$ref`` in a tenant-supplied schema must stay inside that schema."""
    if isinstance(node, Mapping):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            raise WorkflowCompileError(
                WORKFLOW_SCHEMA_INVALID,
                f"output_schema may not reference {reference!r}; only local references are allowed",
                path=path,
            )
        for value in node.values():
            _reject_non_local_references(value, path=path)
    elif isinstance(node, list):
        for value in node:
            _reject_non_local_references(value, path=path)


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
    normalized: dict[str, Any] = dict(_strict_json_copy(definition))
    inputs = normalized.setdefault("inputs", {})
    if isinstance(inputs, dict):
        for declaration in inputs.values():
            if isinstance(declaration, dict):
                declaration.setdefault("required", False)

    limits = normalized.get("limits")
    if not isinstance(limits, dict):
        limits = {}
        normalized["limits"] = limits
    for key, value in _LIMIT_DEFAULTS.items():
        limits.setdefault(key, value)

    output = normalized.get("output")
    if not isinstance(output, dict):
        normalized["output"] = dict(_OUTPUT_DEFAULT)

    nodes = normalized.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            retry = node.get("retry")
            if isinstance(retry, dict):
                retry.setdefault("max_attempts", 1)
                retry.setdefault("backoff_sec", 5)
            config = node.get("config")
            if node.get("type") == "approval" and isinstance(config, dict):
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
        "workflow references must be " + " or ".join(REFERENCE_SYNTAX.values()),
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
        kind = CEL_REFERENCE_NAMESPACES[match.group(1)][0]
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

    @staticmethod
    def _check_embedded_schema(schema: Any, *, path: str) -> None:
        """Validate a node's embedded Draft 7 result schema.

        The contract requires a metaschema check at save time and forbids
        network ``$ref``, file access, and unbounded reference expansion, so the
        schema must be self-contained: every reference stays local to it, which
        also bounds expansion by the document itself.
        """
        if schema is None or isinstance(schema, bool):
            return
        if not isinstance(schema, Mapping):
            raise WorkflowCompileError(
                WORKFLOW_SCHEMA_INVALID,
                "output_schema must be a schema object or boolean",
                path=path,
            )
        _reject_non_local_references(schema, path=path)
        try:
            Draft7Validator.check_schema(schema)
        except SchemaError as exc:
            raise WorkflowCompileError(
                WORKFLOW_SCHEMA_INVALID,
                f"output_schema is not a valid Draft 7 schema: {exc.message}",
                path=path,
            ) from exc

    def check_node_identity(self) -> None:
        collector = _DiagnosticCollector()
        seen: dict[str, str] = {}
        for node in self.nodes:
            node_id = str(node["id"])
            if node_id in seen:
                collector.refuse(
                    WORKFLOW_NODE_DUPLICATE_ID,
                    f"node id {node_id!r} is used more than once",
                    path=f"nodes.{node_id}.id",
                )
                # A repeated node repeats its save_as as well; reporting that
                # second defect would only bury the one that can be fixed.
                continue
            seen[node_id] = node_id
            save_as = node.get("save_as")
            output_key = str(save_as) if save_as is not None else node_id
            producer = self.output_key_to_node.get(output_key)
            if producer is not None:
                collector.refuse(
                    WORKFLOW_SAVE_AS_DUPLICATE,
                    f"output key {output_key!r} is produced by both {producer!r} and {node_id!r}",
                    path=f"nodes.{node_id}.save_as",
                )
            self.output_key_to_node[output_key] = node_id
            if save_as is not None:
                self.save_as_by_node[node_id] = str(save_as)
            if str(node.get("type")) == "ask":
                self._check_ask_fields(collector, node_id, node)
        collector.raise_if_any("identity")

    def _check_ask_fields(
        self, collector: _DiagnosticCollector, node_id: str, node: Mapping[str, Any]
    ) -> None:
        """An ask node's field names are the keys of the submitted mapping.

        Two fields sharing a name would collide there and the later one would
        silently win, so the names must be unique inside one node.  The schema
        constrains one field at a time and cannot state this.
        """
        config = node.get("config") or {}
        seen: set[str] = set()
        for index, field in enumerate(config.get("fields") or ()):
            if not isinstance(field, Mapping):
                continue
            name = str(field.get("name"))
            if name in seen:
                collector.refuse(
                    WORKFLOW_ASK_FIELDS_DUPLICATE,
                    f"ask node {node_id!r} declares field {name!r} twice",
                    path=f"nodes.{node_id}.config.fields[{index}].name",
                )
            seen.add(name)

    def _approval_target(self, node: Mapping[str, Any]) -> str | None:
        config = node.get("config") or {}
        target = config.get("target_node_id")
        return str(target) if target else None

    def check_edges(self) -> None:
        collector = _DiagnosticCollector()
        seen_pairs: set[tuple[str, str]] = set()
        for index, edge in enumerate(self.edge_specs):
            source = str(edge["from"])
            target = str(edge["to"])
            when = edge.get("when")
            path = f"edges[{index}]"
            if source not in self.node_by_id or target not in self.node_by_id:
                if source not in self.node_by_id:
                    collector.refuse(
                        WORKFLOW_EDGE_UNKNOWN_NODE,
                        f"edge source {source!r} is not a node",
                        path=path,
                    )
                if target not in self.node_by_id:
                    collector.refuse(
                        WORKFLOW_EDGE_UNKNOWN_NODE,
                        f"edge target {target!r} is not a node",
                        path=path,
                    )
                # Without both endpoints the edge has no type and no place in
                # the graph, so nothing below can be checked against it.
                continue
            if source == target:
                collector.refuse(
                    WORKFLOW_EDGE_SELF_LOOP, f"node {source!r} cannot point at itself", path=path
                )
                continue
            if (source, target) in seen_pairs:
                collector.refuse(
                    WORKFLOW_EDGE_DUPLICATE,
                    f"edge {source!r} -> {target!r} is duplicated",
                    path=path,
                )
                continue
            seen_pairs.add((source, target))
            node_type = str(self.node_by_id[source].get("type"))
            if when is None and node_type == "condition":
                collector.refuse(
                    WORKFLOW_CONDITION_EDGES,
                    f"condition node {source!r} needs both a true and a false edge",
                    path=path,
                )
            if when is not None and node_type != "condition":
                collector.refuse(
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
                whens = sorted(edge.when or "" for edge in explicit)
                if whens != ["false", "true"]:
                    collector.refuse(
                        WORKFLOW_CONDITION_EDGES,
                        f"condition node {node_id!r} needs exactly one true and one false edge",
                        path=f"nodes.{node_id}.config.expression",
                    )
                continue
            # Every outgoing edge of a non-condition node is activated, so
            # several of them are a parallel split rather than an error.
            approval_target = self._approval_target(node)
            if approval_target is None:
                continue
            if node_type != "approval":
                collector.refuse(
                    WORKFLOW_APPROVAL_TARGET,
                    f"node {node_id!r} is not an approval node and cannot declare target_node_id",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
                # The field is meaningless here, so neither the edge conflict nor
                # the target itself is worth a second diagnostic.
                continue
            if explicit:
                collector.refuse(
                    WORKFLOW_APPROVAL_EDGES,
                    f"approval node {node_id!r} declares target_node_id and an outgoing edge",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
                continue
            if approval_target not in self.node_by_id:
                collector.refuse(
                    WORKFLOW_APPROVAL_TARGET,
                    f"approval target {approval_target!r} is not a node",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
                continue
            self.effective_edges.append(
                CompiledEdge(node_id, approval_target, when=None, implicit=True)
            )

        for compiled_edge in self.effective_edges:
            self.outgoing[compiled_edge.from_node_id].append(compiled_edge)
            self.incoming[compiled_edge.to_node_id].append(compiled_edge)

        # An explicit output node is the run's result: at most one, and nothing
        # may follow it (a successor would make "what the run produced" ambiguous).
        output_nodes = sorted(
            str(node["id"]) for node in self.nodes if str(node.get("type")) == "output"
        )
        if len(output_nodes) > 1:
            collector.refuse(
                WORKFLOW_OUTPUT_DUPLICATE,
                f"a workflow declares at most one output node; found {len(output_nodes)}",
                details={"output_nodes": output_nodes},
            )
        for node_id in output_nodes:
            if self.outgoing[node_id]:
                collector.refuse(
                    WORKFLOW_OUTPUT_NOT_TERMINAL,
                    f"output node {node_id!r} must be terminal",
                    path=f"nodes.{node_id}.config.value",
                )
        collector.raise_if_any("edges")

    def check_topology(self) -> None:
        collector = _DiagnosticCollector()
        entries = sorted(node_id for node_id in self.node_ids if not self.incoming[node_id])
        node_type_by_id = {str(node["id"]): str(node.get("type")) for node in self.nodes}
        # Explicit ``input`` nodes are sources of the graph by design: several may
        # feed one pipeline, so only the *other* roots are counted as entries.
        # A definition without input nodes keeps the original rule exactly.
        non_input_entries = [
            node_id for node_id in entries if node_type_by_id.get(node_id) != "input"
        ]
        if len(non_input_entries) > 1:
            collector.refuse(
                WORKFLOW_ENTRY_COUNT,
                "workflow needs exactly one entry node besides its explicit input nodes;"
                f" found {len(non_input_entries)}",
                details={"entry_candidates": non_input_entries},
            )
            # Reachability and ancestry are defined relative to that one entry.
            collector.raise_if_any("topology")
            return
        if not entries:
            # Every node has an incoming edge, so the graph has no source at all:
            # that is precisely a cycle, which the pass below names node by node.
            # Indexing here would abort the compile with an IndexError instead of
            # returning that diagnostic.
            self.entry_node_id = ""
        else:
            self.entry_node_id = non_input_entries[0] if non_input_entries else entries[0]

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
            collector.refuse(
                WORKFLOW_CYCLE,
                "workflow graph contains a cycle",
                details={"cycle_nodes": blocked},
            )
            # Without a topological order there are no ancestors to check against.
            collector.raise_if_any("topology")
            return
        self.topological_order = tuple(order)

        # Every source starts the run: explicit ``input`` nodes are roots by
        # design, so reachability is measured from all of them.  A definition
        # without input nodes has exactly one root and behaves as before.
        reached = set(entries)
        pending = list(entries)
        while pending:
            current = pending.pop()
            for edge in self.outgoing[current]:
                if edge.to_node_id not in reached:
                    reached.add(edge.to_node_id)
                    pending.append(edge.to_node_id)
        unreachable = sorted(set(self.node_ids) - reached)
        if unreachable:
            collector.refuse(
                WORKFLOW_UNREACHABLE,
                "workflow has nodes that are not reachable from its sources",
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
                collector.refuse(
                    WORKFLOW_APPROVAL_TARGET,
                    f"approval target {target!r} is not downstream of {node_id!r}",
                    path=f"nodes.{node_id}.config.target_node_id",
                )
        collector.raise_if_any("topology")

    # -- references -------------------------------------------------------- #

    def _check_reference(
        self,
        collector: _DiagnosticCollector,
        kind: str,
        name: str,
        *,
        node_id: str,
        path: str,
        scope: str,
    ) -> None:
        if kind == "input":
            if name not in self.input_names:
                collector.refuse(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference inputs.{name} is not a declared input",
                    path=path,
                    details={"scope": scope},
                )
            return
        if kind == "node":
            producer = self.node_by_id.get(name)
            if producer is None:
                collector.refuse(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference nodes.{name}.output is not a node in this workflow",
                    path=path,
                    details={"scope": scope},
                )
                return
            owner: str | None = name
        else:
            owner = self.output_key_to_node.get(name)
            if owner is None:
                collector.refuse(
                    WORKFLOW_REFERENCE_UNKNOWN,
                    f"reference outputs.{name} has no unique producer",
                    path=path,
                    details={"scope": scope},
                )
                return
        if owner not in self.ancestors[node_id]:
            collector.refuse(
                WORKFLOW_REFERENCE_NOT_UPSTREAM,
                f"reference to {name!r} is not upstream of node {node_id!r}",
                path=path,
                details={"scope": scope, "producer": owner},
            )

    def check_references(self) -> None:
        collector = _DiagnosticCollector()
        for node in self.nodes:
            node_id = str(node["id"])
            node_type = str(node.get("type"))
            config = node.get("config") or {}
            if node_type == "input":
                # An explicit input node surfaces one declared input into the
                # graph; naming an undeclared one would read as "no input" at run
                # time, so it is refused while the definition is still a draft.
                declared = str(config.get("input"))
                if declared not in self.input_names:
                    collector.refuse(
                        WORKFLOW_INPUT_NODE_UNKNOWN,
                        f"input node {node_id!r} names undeclared input {declared!r}",
                        path=f"nodes.{node_id}.config.input",
                    )
                continue
            field = TEMPLATE_FIELD_PATHS.get(node_type)
            if field is not None and field in config:
                field_path = f"nodes.{node_id}.config.{field}"
                try:
                    references = iter_template_references(config[field], path=field_path)
                except WorkflowCompileError as exc:
                    collector.absorb(exc)
                    references = []
                for kind, name in references:
                    self._check_reference(
                        collector,
                        kind,
                        name,
                        node_id=node_id,
                        path=field_path,
                        scope="template",
                    )
            if node_type in {"condition", "transform"}:
                expression = str(config.get("expression") or "")
                expression_path = f"nodes.{node_id}.config.expression"
                try:
                    self.cel_evidence[expression_path] = _probe_cel(
                        expression, path=expression_path
                    )
                except WorkflowCompileError as exc:
                    collector.absorb(exc)
                try:
                    self._check_embedded_schema(
                        config.get("output_schema"),
                        path=f"nodes.{node_id}.config.output_schema",
                    )
                except WorkflowCompileError as exc:
                    collector.absorb(exc)
                for kind, name in iter_cel_references(expression):
                    self._check_reference(
                        collector, kind, name, node_id=node_id, path=expression_path, scope="cel"
                    )
        collector.raise_if_any("references")

    # -- semantics --------------------------------------------------------- #

    def check_semantics(
        self,
        resolver: WorkflowSemanticResolver | None,
        *,
        require_semantic_resolution: bool,
    ) -> str:
        needed = any(
            str(node.get("type")) in {"tool", "llm", "approval", "ask", "knowledge"}
            for node in self.nodes
        )
        if not needed:
            return "not_required"
        if resolver is None:
            if require_semantic_resolution:
                raise WorkflowCompileError(
                    WORKFLOW_DEPENDENCY_UNAVAILABLE,
                    "workflow definition needs semantic resolution but no resolver is configured",
                )
            return "skipped"
        collector = _DiagnosticCollector()
        for node in self.nodes:
            node_id = str(node["id"])
            node_type = str(node.get("type"))
            config = node.get("config") or {}
            if node_type == "tool":
                self._apply_decision(
                    collector,
                    resolver.check_tool(
                        str(config.get("tool_name")), config.get("parameters") or {}
                    ),
                    fallback_code=WORKFLOW_TOOL_UNAVAILABLE,
                    fallback_message=f"tool {config.get('tool_name')!r} is not available",
                    path=f"nodes.{node_id}.config.tool_name",
                )
            elif node_type == "llm":
                self._apply_decision(
                    collector,
                    resolver.check_model(config.get("model")),
                    fallback_code=WORKFLOW_MODEL_NOT_CONFIGURED,
                    fallback_message="no tenant model is configured for this llm node",
                    path=f"nodes.{node_id}.config.model",
                )
                for index, knowledge_base_id in enumerate(config.get("knowledge_base_ids") or []):
                    self._apply_decision(
                        collector,
                        resolver.check_knowledge_base(str(knowledge_base_id)),
                        fallback_code=WORKFLOW_KNOWLEDGE_BASE_UNKNOWN,
                        fallback_message="knowledge base is not visible in this tenant",
                        path=f"nodes.{node_id}.config.knowledge_base_ids[{index}]",
                    )
            elif node_type == "knowledge":
                # Retrieval is its own step now; its sources must be reachable for
                # the caller exactly like an llm node's knowledge bases.
                for index, knowledge_base_id in enumerate(config.get("knowledge_base_ids") or []):
                    self._apply_decision(
                        collector,
                        resolver.check_knowledge_base(str(knowledge_base_id)),
                        fallback_code=WORKFLOW_KNOWLEDGE_BASE_UNKNOWN,
                        fallback_message="knowledge base is not visible in this tenant",
                        path=f"nodes.{node_id}.config.knowledge_base_ids[{index}]",
                    )
            elif node_type == "approval":
                # Only "nobody can approve" blocks a publish: a declared approver
                # who has left is tolerated here and surfaces at run time as a
                # node failure (APPROVAL_NO_VALID_APPROVER) instead.
                valid = 0
                for approver in config.get("approver_user_ids") or []:
                    decision = resolver.check_approver(str(approver))
                    if decision is None or decision.ok:
                        valid += 1
                if not valid:
                    collector.refuse(
                        ErrorCode.APPROVAL_NO_VALID_APPROVER.value,
                        f"approval node {node_id!r} has no valid approver in this tenant",
                        path=f"nodes.{node_id}.config.approver_user_ids",
                    )
            elif node_type == "ask":
                # The same rule the approval applies: only "nobody can answer"
                # blocks a publish.  An assignee who has since left is tolerated
                # here and surfaces at run time as a node failure
                # (ASK_NO_VALID_ASSIGNEE), so an unrelated departure cannot make
                # an otherwise sound workflow unpublishable.
                answerable = 0
                for assignee in config.get("assignee_user_ids") or []:
                    decision = resolver.check_approver(str(assignee))
                    if decision is None or decision.ok:
                        answerable += 1
                if not answerable:
                    collector.refuse(
                        ErrorCode.ASK_NO_VALID_ASSIGNEE.value,
                        f"ask node {node_id!r} has no valid assignee in this tenant",
                        path=f"nodes.{node_id}.config.assignee_user_ids",
                    )
        collector.raise_if_any("semantics")
        return "passed"

    @staticmethod
    def _apply_decision(
        collector: _DiagnosticCollector,
        decision: SemanticDecision | None,
        *,
        fallback_code: str,
        fallback_message: str,
        path: str,
    ) -> None:
        if decision is None or decision.ok:
            return
        collector.refuse(
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


def _schema_diagnostics(errors: Sequence[ValidationError]) -> tuple[WorkflowDiagnostic, ...]:
    """Every schema error as a diagnostic, in the aggregate's stable order."""
    return tuple(
        sorted(
            (
                WorkflowDiagnostic(
                    WORKFLOW_SCHEMA_INVALID,
                    error.message,
                    path=".".join(str(part) for part in error.absolute_path),
                )
                for error in errors
            ),
            key=lambda diagnostic: (diagnostic.path, diagnostic.code),
        )
    )


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
        diagnostics = _schema_diagnostics(errors)
        primary = diagnostics[0]
        raise WorkflowCompileError(
            primary.code,
            primary.message,
            path=primary.path,
            details={
                "stage": "schema",
                "errors": [
                    {
                        "path": ".".join(str(part) for part in error.absolute_path),
                        "message": error.message,
                    }
                    for error in errors[:MAX_DIAGNOSTICS]
                ],
            },
            diagnostics=diagnostics,
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
    "ASK_FIELD_TYPES",
    "CANDIDATE_VERSION_ORIGINS",
    "CEL_REFERENCE_NAMESPACES",
    "CompiledEdge",
    "CompiledNode",
    "CompiledWorkflow",
    "DIAGNOSTIC_HINT_PREFIX",
    "MAX_DIAGNOSTICS",
    "MAX_REFERENCE_LENGTH",
    "MAX_TEMPLATE_PLACEHOLDERS",
    "NODE_TYPES",
    "REFERENCE_SYNTAX",
    "SemanticDecision",
    "TEMPLATE_FIELD_PATHS",
    "VERSION_ORIGINS",
    "WORKFLOW_COMPILER_VERSION",
    "WORKFLOW_SCHEMA_ENV",
    "WORKFLOW_SCHEMA_FILENAME",
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowCompileError",
    "WorkflowDiagnostic",
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
