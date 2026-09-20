"""Deterministic WorkBuddy execution engine and tenant runtime service.

The engine is synchronous, side-effect free and replayable: a run is a pure
function of the locked workflow snapshot, the workflow inputs, the recorded
approval decisions and the already-recorded step results. Transform and
condition nodes are evaluated through the bounded CEL sandbox; tool, LLM and
private-chat nodes only run through a trusted adapter port and otherwise fail
closed (``DEPENDENCY_UNAVAILABLE`` / ``MODEL_NOT_CONFIGURED``) — no external
write is ever simulated.

All durable facts live in PostgreSQL through ``WorkBuddyRuntimeRepo`` inside the
audited ``workbuddy_transaction`` context; Redis or any queue may only carry
delivery hints derived from those rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_execution_feedback import (
    ExecutionFeedbackRepo,
    ExecutionFeedbackRow,
)
from octop.infra.db.repos.workbuddy_runtime import (
    ApprovalCandidateRow,
    ApprovalRequestRow,
    AuditLogRow,
    ChatMessageRow,
    ChatSessionRow,
    ExecutionClaim,
    ExecutionRow,
    InputAssigneeRow,
    InputRequestRow,
    JobRow,
    NotificationRow,
    OutputReviewReviewerRow,
    OutputReviewRow,
    ReconciliationRow,
    WorkBuddyRuntimeRepo,
    canonical_json,
    execution_lease_name,
    new_runtime_id,
    require_postgres,
    runtime_transaction,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.attribution import attribute_corrections
from octop.infra.workbuddy.cel_sandbox import CELSandboxError, evaluate_cel
from octop.infra.workbuddy.log_redaction import register_secret
from octop.infra.workbuddy.roles import TENANT_ADMIN_ROLES
from octop.infra.workbuddy.workflow_compiler import (
    ASK_FIELD_TYPES,
    NODE_TYPES,
    CompiledWorkflow,
    WorkflowCompileError,
    compile_stored_definition,
    parse_reference,
)

logger = logging.getLogger(__name__)

# ``NODE_TYPES`` is imported, not re-declared: the compiler owns the node-type
# contract because it validates a definition against the schema.  A second list
# here could only drift — and it did: it was missing the input/knowledge/output
# types this module already executes.  The name stays in ``__all__`` so callers
# that import it from the runtime keep working.
TERMINAL_EXECUTION_STATUSES = frozenset({"success", "failed", "partial", "canceled"})
EXECUTION_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_approval",
        "waiting_input",
        "waiting_reconciliation",
        "success",
        "failed",
        "partial",
        "canceled",
    }
)
CANCELLABLE_EXECUTION_STATUSES = ("queued", "running", "waiting_approval", "waiting_input")
APPROVAL_TOKEN_TTL_SECONDS = 120
# A monthly execution reservation must outlive any legitimate wait (approval,
# reconciliation) inside its own period; anything shorter would let the monthly
# allowance free itself while the run that consumed it is still unresolved.
EXECUTION_RESERVATION_TTL_SECONDS = 32 * 24 * 60 * 60
DEFAULT_MAX_STEPS = 50
MAX_STEPS_CAP = 200
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
MAX_WORKFLOW_INPUT_BYTES = 1_048_576
MAX_DEFINITION_NODES = 100
MAX_DEFINITION_EDGES = 4_950

APPROVAL_DECISIONS = frozenset({"approved", "rejected"})
RECONCILIATION_DECISIONS = frozenset({"confirmed_success", "confirmed_failed"})

# Restricted one-pass template placeholder, matching the compiler's grammar.
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _not_found(message: str = "workbuddy resource not found") -> OctopError:
    return OctopError(ErrorCode.RESOURCE_NOT_FOUND, message)


def _invalid(message: str) -> OctopError:
    return OctopError(ErrorCode.WORKBUDDY_VALIDATION_FAILED, message)


# ---------------------------------------------------------------------------
# Pure graph model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    type: str
    name: str
    config: Mapping[str, Any]
    save_as: str | None
    # The node's retry budget (``max_attempts``/``backoff_sec``), already
    # validated and defaulted by the compiler, or None when the definition asked
    # for none. The engine honours it only for failures that provably dispatched
    # nothing.
    retry: Mapping[str, Any] | None = None

    @property
    def output_key(self) -> str:
        return self.save_as or self.id

    @property
    def expression(self) -> str | None:
        raw = self.config.get("expression")
        return str(raw) if isinstance(raw, str) and raw else None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    when: str | None


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    version_id: str
    definition_sha256: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    max_steps: int
    max_output_bytes: int

    def node(self, node_id: str) -> GraphNode | None:
        for candidate in self.nodes:
            if candidate.id == node_id:
                return candidate
        return None

    def outgoing(self, node_id: str) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.source == node_id)


@dataclass(frozen=True, slots=True)
class StepOutcome:
    node_id: str
    node_type: str
    status: str
    save_as: str | None
    output: Any = None
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False
    skip_reason: str | None = None
    started_at: float | None = None
    duration_ms: int | None = None
    tokens: int = 0
    # How many extra calls this attempt made before the step settled (0 = the
    # declared retry budget was never needed).
    retries: int = 0
    # What this attempt was dispatched with, and what the model call reported for
    # it. ``None`` means the engine had no such fact: a skipped or replayed row
    # dispatched nothing, an approval node calls nothing, and an adapter that
    # reports no usage stores no usage.
    input: Mapping[str, Any] | None = None
    token_usage: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EdgeOutcome:
    source: str
    target: str
    branch: str | None
    taken: bool
    failed: bool = False


class UnresolvedToolOutcome(Exception):
    """An external write was dispatched but its outcome cannot be determined.

    The trusted adapter reports what it knows at the moment it gives up: the
    stable operation key, when it dispatched, the tool revision and resolved
    parameter digest it used, and any external reference that can be verified
    later. The engine parks the step for reconciliation instead of retrying, so
    the same write is never sent twice by accident.
    """

    def __init__(
        self,
        *,
        operation_key: str,
        external_request_id: str | None = None,
        dispatched_at: str | None = None,
        tool_revision: str | None = None,
        parameters_digest: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(detail or operation_key)
        self.operation_key = operation_key
        self.external_request_id = external_request_id
        self.dispatched_at = dispatched_at
        self.tool_revision = tool_revision
        self.parameters_digest = parameters_digest
        self.detail = detail

    def facts(self) -> dict[str, Any]:
        """The record the execution keeps while the outcome is unknown."""
        return {
            "operation_key": self.operation_key,
            "external_request_id": self.external_request_id,
            "dispatched_at": self.dispatched_at,
            "tool_revision": self.tool_revision,
            "parameters_digest": self.parameters_digest,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReplayState:
    """Results already recorded for this execution (resume must not re-run them)."""

    outputs: Mapping[str, Any] = field(default_factory=dict)
    skipped: frozenset[str] = frozenset()
    failed: Mapping[str, tuple[str | None, str | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphRun:
    status: str
    steps: tuple[StepOutcome, ...]
    edges: tuple[EdgeOutcome, ...]
    outputs: dict[str, Any]
    tokens: int = 0
    error_code: str | None = None
    error_message: str | None = None
    waiting_approval_node_id: str | None = None
    waiting_input_node_id: str | None = None
    reconciliation_node_id: str | None = None
    approval_candidates: dict[str, tuple[tuple[int, str | None], ...]] = field(default_factory=dict)
    input_assignees: dict[str, tuple[tuple[int, str | None], ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatReply:
    content: str
    model_revision: str | None = None
    usage: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CanaryRoute:
    """How one execution is routed while a proposal evaluates a candidate.

    The cohort is decided by the deterministic bucket, never by the runner, and
    the candidate runs the candidate version while the baseline runs the version
    the proposal fixed as its base.
    """

    proposal_id: str
    cohort: Literal["canary", "baseline"]
    version_id: str
    bucket: int
    ratio_basis_points: int
    subject: str


class CanaryDirectory(Protocol):
    """Read side of the active canary, owned by the proposals slice."""

    def route(self, *, tenant_id: str, workflow_id: str, subject: str) -> CanaryRoute | None: ...


class NoCanaryEvaluation:
    """Default: no workflow is under evaluation here."""

    def route(self, *, tenant_id: str, workflow_id: str, subject: str) -> CanaryRoute | None:
        return None


NO_CANARY_EVALUATION = NoCanaryEvaluation()


class SideEffectPort(Protocol):
    """Trusted adapter boundary for anything that leaves the process."""

    def execute_tool(
        self, *, node: GraphNode, activation: Mapping[str, Any], idempotency_key: str
    ) -> Any: ...

    def execute_llm(self, *, node: GraphNode, activation: Mapping[str, Any]) -> Any: ...

    def respond_chat(
        self, *, session_id: str, message: str, history: Sequence[ChatMessageRow]
    ) -> ChatReply: ...


class ReplayUnavailable(RuntimeError):
    """A shadow run needed a recording that does not exist.

    Shadow never falls back to a live call: a missing recording fails the run,
    which is exactly what keeps replay-only evidence honest.
    """


class ReplaySideEffects:
    """Answers every step from recorded outputs, never from a live system.

    The port holds no live adapter at all, so a shadow run cannot reach an
    external system even if it wanted to: the only thing it can do with an
    unrecorded step is fail.
    """

    def __init__(self, recordings: Mapping[str, Any]) -> None:
        self._recordings = dict(recordings)
        self.replayed: list[str] = []

    def execute_tool(
        self, *, node: GraphNode, activation: Mapping[str, Any], idempotency_key: str
    ) -> Any:
        return self._replay(node)

    def execute_llm(self, *, node: GraphNode, activation: Mapping[str, Any]) -> Any:
        return self._replay(node)

    def respond_chat(
        self, *, session_id: str, message: str, history: Sequence[ChatMessageRow]
    ) -> Any:
        raise ReplayUnavailable("chat has no recording to replay")

    def _replay(self, node: GraphNode) -> Any:
        if node.id not in self._recordings:
            raise ReplayUnavailable(f"node '{node.id}' has no recorded response")
        self.replayed.append(node.id)
        return self._recordings[node.id]


class ShadowRunner:
    """Replays a candidate definition against recorded responses.

    A shadow run evaluates the *candidate* graph on the recordings of an earlier
    production execution, so it can fail or succeed on real inputs without
    sending anything to the outside world. Its result is evidence for the shadow
    phase; it is never a canary sample.
    """

    def __init__(self, versions: WorkflowVersionSource, db: DatabasePool | None = None) -> None:
        self._versions = versions

    def run(
        self,
        ctx: WorkBuddyDbContext,
        *,
        workflow_id: str,
        candidate_version_id: str,
        inputs: Mapping[str, Any],
        recordings: Mapping[str, Any],
    ) -> dict[str, Any]:
        locked = self._versions.load_version(ctx, workflow_id, candidate_version_id)
        graph = compile_locked_definition(
            locked.definition, locked.definition_sha256, version_id=locked.version_id
        )
        port = ReplaySideEffects(recordings)
        run = run_graph(graph, inputs=dict(inputs), effects=port, execution_id="shadow")
        record = {
            "status": run.status,
            "outputs": run.outputs,
            "steps": [
                {"node_id": step.node_id, "status": step.status, "output": step.output}
                for step in run.steps
            ],
            "replayed": sorted(port.replayed),
        }
        return {
            **record,
            "evidence_hash": hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "replay_only": True,
            "live_side_effects": 0,
            "settled": True,
        }


class UnavailableSideEffects:
    """Fail-closed default: no trusted adapter is configured in this deployment."""

    def execute_tool(
        self, *, node: GraphNode, activation: Mapping[str, Any], idempotency_key: str
    ) -> Any:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            f"no trusted tool adapter is configured for node '{node.id}'",
        )

    def execute_llm(self, *, node: GraphNode, activation: Mapping[str, Any]) -> Any:
        raise OctopError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"no approved model revision is configured for node '{node.id}'",
        )

    def respond_chat(
        self, *, session_id: str, message: str, history: Sequence[ChatMessageRow]
    ) -> ChatReply:
        raise OctopError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            "no approved model revision is configured for private chat",
        )


UNAVAILABLE_SIDE_EFFECTS = UnavailableSideEffects()


@dataclass(frozen=True, slots=True)
class LockedWorkflowVersion:
    workflow_id: str
    version_id: str
    definition_sha256: str
    definition: Mapping[str, Any]


class WorkflowVersionSource(Protocol):
    """Read side of the immutable workflow catalog (owned by the workflow slice)."""

    def load_active(self, ctx: WorkBuddyDbContext, workflow_id: str) -> LockedWorkflowVersion: ...

    def load_version(
        self, ctx: WorkBuddyDbContext, workflow_id: str, version_id: str
    ) -> LockedWorkflowVersion: ...


class UnavailableWorkflowVersions:
    def load_active(self, ctx: WorkBuddyDbContext, workflow_id: str) -> LockedWorkflowVersion:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "workflow version storage is not configured",
        )

    def load_version(
        self, ctx: WorkBuddyDbContext, workflow_id: str, version_id: str
    ) -> LockedWorkflowVersion:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "workflow version storage is not configured",
        )


UNAVAILABLE_WORKFLOW_VERSIONS = UnavailableWorkflowVersions()


class WorkflowCatalogVersions:
    """Locked-version reads from the immutable workflow catalog (workflow slice).

    Reads only; the catalog's own repository is the sole writer of workflow and
    version rows. A missing or invisible workflow yields the uniform not-found.
    """

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _repo(self) -> Any:
        try:
            from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
        except ImportError as exc:  # pragma: no cover - catalog slice not deployed
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "workflow catalog repository is not available",
            ) from exc
        return WorkBuddyWorkflowRepo(self._db)

    @staticmethod
    def _locked(record: Any) -> LockedWorkflowVersion:
        definition = record.definition
        if not isinstance(definition, Mapping):
            raise OctopError(
                ErrorCode.WORKBUDDY_VALIDATION_FAILED,
                "stored workflow version has no definition object",
            )
        return LockedWorkflowVersion(
            workflow_id=str(record.workflow_id),
            version_id=str(record.workflow_version_id),
            definition_sha256=str(record.definition_sha256),
            definition=definition,
        )

    def load_active(self, ctx: WorkBuddyDbContext, workflow_id: str) -> LockedWorkflowVersion:
        record = self._repo().load_active_version(ctx.tenant_id or "", workflow_id)
        if record is None:
            raise _not_found()
        return self._locked(record)

    def load_version(
        self, ctx: WorkBuddyDbContext, workflow_id: str, version_id: str
    ) -> LockedWorkflowVersion:
        record = self._repo().get_version(ctx.tenant_id or "", workflow_id, version_id)
        if record is None:
            raise _not_found()
        return self._locked(record)


def _json_safe(value: Any) -> Any:
    """Convert driver types (UUID/datetime/tuples) into CEL/JSON-safe values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _hash_json(value: Any) -> tuple[str, int]:
    encoded = canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Graph projection of a compiler-validated definition snapshot
# ---------------------------------------------------------------------------


def _limits_of(definition: Mapping[str, Any]) -> tuple[int, int]:
    raw = definition.get("limits")
    limits = raw if isinstance(raw, Mapping) else {}
    max_steps = limits.get("max_steps")
    max_output_bytes = limits.get("max_output_bytes")
    steps = int(max_steps) if isinstance(max_steps, int) else DEFAULT_MAX_STEPS
    output_bytes = (
        int(max_output_bytes) if isinstance(max_output_bytes, int) else DEFAULT_MAX_OUTPUT_BYTES
    )
    return max(1, min(steps, MAX_STEPS_CAP)), max(1, output_bytes)


_INPUT_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (Mapping,),
    "array": (Sequence,),
    # A file reference travels as its opaque string handle.
    "file_ref": (str,),
}


def _period_start() -> datetime:
    """The first instant of the current UTC month, for monthly allowances."""
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _validate_execution_inputs(
    definition: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Check the payload against the definition's declared inputs.

    The contract's T06 observation is that missing-required, surplus and
    wrongly-typed inputs are refused *before* the execution starts, so this runs
    before the idempotency lookup, the quota reservation and the run row: a
    refused request must leave no trace and consume no external call.
    """
    declarations = definition.get("inputs") or {}
    if not isinstance(declarations, Mapping):
        declarations = {}
    accepted: dict[str, Any] = {}
    for name in payload:
        if name not in declarations:
            raise _invalid(f"input {name!r} is not declared by this workflow")
    for name, declaration in declarations.items():
        declared_type = (
            str((declaration or {}).get("type")) if isinstance(declaration, Mapping) else ""
        )
        if name not in payload:
            if isinstance(declaration, Mapping) and "default" in declaration:
                accepted[name] = declaration["default"]
                continue
            if isinstance(declaration, Mapping) and declaration.get("required"):
                raise _invalid(f"required input {name!r} is missing")
            continue
        value = payload[name]
        expected = _INPUT_TYPE_CHECKS.get(declared_type)
        if expected is None:
            accepted[name] = value
            continue
        # ``bool`` is a subclass of ``int``; a boolean is not an integer input.
        if isinstance(value, bool) and declared_type in {"integer", "number"}:
            raise _invalid(f"input {name!r} must be a {declared_type}")
        if declared_type == "array" and isinstance(value, (str, bytes)):
            raise _invalid(f"input {name!r} must be a {declared_type}")
        if not isinstance(value, expected):
            raise _invalid(f"input {name!r} must be a {declared_type}")
        accepted[name] = value
    return accepted


def graph_from_compiled(compiled: CompiledWorkflow, *, version_id: str) -> WorkflowGraph:
    """Project the compiler's canonical output into the runtime graph.

    The compiler owns structural rules (schema, topology, references, single
    entry, condition fan-out). The engine only adds its scheduling view, so no
    second definition parser exists.
    """
    definition = compiled.definition if isinstance(compiled.definition, Mapping) else {}
    max_steps, max_output_bytes = _limits_of(definition)
    nodes = tuple(
        GraphNode(
            id=node.node_id,
            type=node.node_type,
            name=node.name,
            config=dict(node.config) if isinstance(node.config, Mapping) else {},
            save_as=node.save_as,
            retry=dict(node.retry) if node.retry else None,
        )
        for node in compiled.nodes
    )
    edges = tuple(
        GraphEdge(source=edge.from_node_id, target=edge.to_node_id, when=edge.when)
        for edge in compiled.edges
    )
    return WorkflowGraph(
        version_id=version_id,
        definition_sha256=compiled.definition_sha256,
        nodes=nodes,
        edges=edges,
        max_steps=max_steps,
        max_output_bytes=max_output_bytes,
    )


def _compile_error(exc: WorkflowCompileError) -> OctopError:
    """Surface a compiler verdict with the shared WorkBuddy error vocabulary."""
    try:
        code = ErrorCode(str(exc.code))
    except ValueError:
        code = ErrorCode.WORKBUDDY_VALIDATION_FAILED
    return OctopError(code, str(exc.message))


def compile_locked_definition(
    definition: Mapping[str, Any],
    expected_sha256: str,
    *,
    version_id: str,
) -> WorkflowGraph:
    """Re-verify one immutable locked version and project it for execution."""
    try:
        compiled = compile_stored_definition(definition, expected_sha256)
    except WorkflowCompileError as exc:
        raise _compile_error(exc) from exc
    return graph_from_compiled(compiled, version_id=version_id)


# ---------------------------------------------------------------------------
# Deterministic engine
# ---------------------------------------------------------------------------


def render_template(
    value: Any,
    *,
    inputs: Mapping[str, Any],
    node_results: Mapping[str, Any],
) -> Any:
    """Render a restricted one-pass template tree into a JSON value.

    A string that is exactly one placeholder keeps the referenced value's type;
    a string that mixes text and placeholders interpolates (strings directly,
    every other JSON value as canonical JSON). References are the compiler's
    grammar (``inputs.<name>`` / ``nodes.<node_id>.output``), so an unknown or
    upstream-violating reference can never reach the engine.
    """
    if isinstance(value, str):
        matches = list(_TEMPLATE_RE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].group(0) == value:
            return _resolve_reference(matches[0].group(1), inputs=inputs, node_results=node_results)
        parts: list[str] = []
        cursor = 0
        for match in matches:
            parts.append(value[cursor : match.start()])
            resolved = _resolve_reference(match.group(1), inputs=inputs, node_results=node_results)
            parts.append(resolved if isinstance(resolved, str) else canonical_json(resolved))
            cursor = match.end()
        parts.append(value[cursor:])
        return "".join(parts)
    if isinstance(value, Mapping):
        return {
            str(key): render_template(item, inputs=inputs, node_results=node_results)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [render_template(item, inputs=inputs, node_results=node_results) for item in value]
    return value


def _resolve_reference(
    reference: str, *, inputs: Mapping[str, Any], node_results: Mapping[str, Any]
) -> Any:
    kind, name = parse_reference(str(reference), path="template")
    if kind == "input":
        return inputs.get(name)
    if name not in node_results:
        raise _invalid(f"template reference nodes.{name}.output has no recorded output")
    return node_results[name]


def _activation(
    graph: WorkflowGraph,
    node: GraphNode,
    *,
    inputs: Mapping[str, Any],
    bindings: Mapping[str, Any],
    input_value: Any = None,
) -> dict[str, Any]:
    """CEL activation: declared inputs, upstream ``save_as`` bindings, node context."""
    return {
        "inputs": _json_safe(inputs),
        "outputs": _json_safe(bindings),
        "input": _json_safe(input_value),
        "node": {"id": node.id, "name": node.name, "type": node.type},
        "workflow": {
            "version_id": graph.version_id,
            "definition_sha256": graph.definition_sha256,
        },
    }


def _evaluate(expression: str, activation: Mapping[str, Any], node: GraphNode) -> Any:
    try:
        result = evaluate_cel(expression, dict(activation))
    except CELSandboxError as exc:
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
            f"CEL evaluation failed for node '{node.id}': {exc.message}",
        ) from exc
    return result.value


# The contract's retry range: ``max_attempts`` counts the first call, and the
# backoff is bounded so a run cannot sleep past its lease.
MAX_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 5
MAX_RETRY_BACKOFF_SECONDS = 60

# A failure that provably dispatched nothing may be retried inside the node's
# budget: the dependency was unreachable, or the request was refused before it
# left the process. Anything else may already have reached the provider, and the
# contract forbids re-sending an external write without a registered idempotency
# guarantee -- the platform tool registry carries no such declaration, so the
# engine never auto-retries those (an *unknown* outcome is parked for
# reconciliation instead, by the adapter raising ``UnresolvedToolOutcome``).
RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
    }
)


def _schema_violation(schema: Mapping[str, Any], value: Any) -> str | None:
    """Why ``value`` does not satisfy ``schema``, or None when it does."""
    import jsonschema

    try:
        jsonschema.validate(value, dict(schema))
    except jsonschema.ValidationError as exc:
        return str(exc.message)
    except jsonschema.SchemaError as exc:  # pragma: no cover - the registry checks this
        return f"registered schema is invalid: {exc.message}"
    return None


def _retry_budget(node: GraphNode) -> tuple[int, int]:
    """Clamped ``(max_attempts, backoff_sec)`` for one node; 1 means no retry."""
    raw = node.retry if isinstance(node.retry, Mapping) else {}
    attempts = raw.get("max_attempts", 1)
    backoff = raw.get("backoff_sec", DEFAULT_RETRY_BACKOFF_SECONDS)
    max_attempts = 1
    if isinstance(attempts, int) and not isinstance(attempts, bool):
        max_attempts = min(max(attempts, 1), MAX_RETRY_ATTEMPTS)
    seconds = DEFAULT_RETRY_BACKOFF_SECONDS
    if isinstance(backoff, int) and not isinstance(backoff, bool):
        seconds = min(max(backoff, 1), MAX_RETRY_BACKOFF_SECONDS)
    return max_attempts, seconds


def _sleep_backoff(seconds: int, should_stop: Callable[[], bool] | None) -> bool:
    """Wait between attempts; False once a cancel has landed."""
    remaining = float(seconds)
    while remaining > 0:
        if should_stop is not None and should_stop():
            return False
        slice_seconds = min(remaining, 1.0)
        time.sleep(slice_seconds)
        remaining -= slice_seconds
    return should_stop is None or not should_stop()


def run_graph(
    graph: WorkflowGraph,
    *,
    inputs: Mapping[str, Any],
    replay: ReplayState | None = None,
    decisions: Mapping[str, str] | None = None,
    answers: Mapping[str, Mapping[str, Any]] | None = None,
    expired_questions: Collection[str] | None = None,
    effects: SideEffectPort | None = None,
    resolve_approvers: Callable[[GraphNode], Sequence[tuple[int, str | None]]] | None = None,
    resolve_assignees: Callable[[GraphNode], Sequence[tuple[int, str | None]]] | None = None,
    execution_id: str = "",
    on_step_start: Callable[[GraphNode], None] | None = None,
    on_step_settled: Callable[[StepOutcome], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_step_dispatch: Callable[[GraphNode], None] | None = None,
    resolve_tool_declaration: Callable[[GraphNode], ToolDeclaration | None] | None = None,
    retrieve_knowledge: Callable[[GraphNode, Mapping[str, Any]], Any] | None = None,
) -> GraphRun:
    """Execute the graph once, deterministically, and aggregate the outcome.

    Scheduling rule: a node runs as soon as every incoming edge is resolved and
    at least one of them was taken. A condition therefore selects exactly one
    branch, every untaken branch is recorded as skipped, and a join node that
    merges those branches runs exactly once. Results recorded by an earlier
    attempt of the same execution are replayed, never re-run, so resuming after
    an approval cannot duplicate a side effect.

    ``on_step_start`` and ``on_step_settled`` let the caller persist a step the
    moment it starts and the moment it settles, so an observer of a long run sees
    ``running`` rather than waiting for the attempt to commit. ``on_step_dispatch``
    fires immediately before an external call, so the operation's intent is
    durable before the call can leave the process. Replayed outcomes are not
    announced: they belong to the attempt that produced them.
    """
    replay = replay or ReplayState()
    decisions = dict(decisions or {})
    answers = {key: dict(value) for key, value in (answers or {}).items()}
    expired = frozenset(str(node_id) for node_id in (expired_questions or ()))
    port: SideEffectPort = effects or UNAVAILABLE_SIDE_EFFECTS

    incoming: dict[str, list[GraphEdge]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        incoming[edge.target].append(edge)

    resolved: dict[str, int] = {node.id: 0 for node in graph.nodes}
    taken_in: dict[str, int] = {node.id: 0 for node in graph.nodes}
    failed_in: dict[str, int] = {node.id: 0 for node in graph.nodes}
    processed: set[str] = set()
    node_results: dict[str, Any] = {}
    bindings: dict[str, Any] = {}
    results: dict[str, Any] = {}
    steps: list[StepOutcome] = []
    edge_outcomes: list[EdgeOutcome] = []
    failure: StepOutcome | None = None  # only the step-limit guard stops a run
    node_failures: list[StepOutcome] = []
    approval_candidates: dict[str, tuple[tuple[int, str | None], ...]] = {}
    input_assignees: dict[str, tuple[tuple[int, str | None], ...]] = {}
    waiting_node: str | None = None
    waiting_input_node: str | None = None
    reconciliation_node: str | None = None
    canceled = False
    executed = 0

    def resolve_edge(edge: GraphEdge, taken: bool, *, failed: bool = False) -> None:
        resolved[edge.target] += 1
        if taken:
            taken_in[edge.target] += 1
        if failed:
            failed_in[edge.target] += 1
        edge_outcomes.append(
            EdgeOutcome(
                source=edge.source,
                target=edge.target,
                branch=edge.when,
                taken=taken,
                failed=failed,
            )
        )

    def propagate_skip(node: GraphNode, *, failed: bool = False) -> None:
        for edge in graph.outgoing(node.id):
            resolve_edge(edge, False, failed=failed)

    def propagate_taken(node: GraphNode) -> None:
        for edge in graph.outgoing(node.id):
            resolve_edge(edge, True)

    def propagate_branch(node: GraphNode, branch: str) -> None:
        for edge in graph.outgoing(node.id):
            resolve_edge(edge, edge.when == branch)

    def record(
        node: GraphNode,
        status: str,
        *,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        replayed: bool = False,
        skip_reason: str | None = None,
        timing: tuple[float, int] | None = None,
        tokens: int = 0,
        retries: int = 0,
        input: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> StepOutcome:
        started_at, duration_ms = timing if timing is not None else (None, None)
        outcome = StepOutcome(
            node_id=node.id,
            node_type=node.type,
            status=status,
            save_as=node.save_as,
            output=output,
            error_code=error_code,
            error_message=error_message,
            replayed=replayed,
            skip_reason=skip_reason,
            started_at=started_at,
            duration_ms=duration_ms,
            tokens=int(tokens or 0),
            retries=int(retries or 0),
            input=input,
            token_usage=token_usage,
        )
        steps.append(outcome)
        if on_step_settled is not None and not replayed:
            on_step_settled(outcome)
        return outcome

    def store(node: GraphNode, value: Any) -> None:
        node_results[node.id] = value
        results[node.output_key] = value
        if node.save_as:
            bindings[node.save_as] = value

    # Replay work already recorded by an earlier attempt of this execution.
    for node in graph.nodes:
        if node.id in replay.skipped:
            processed.add(node.id)
            record(node, "skipped", replayed=True)
            propagate_skip(node)
            continue
        if node.id not in replay.outputs:
            continue
        value = replay.outputs[node.id]
        branch: str | None = None
        if node.type == "condition":
            raw = value.get("branch") if isinstance(value, Mapping) else None
            if raw not in {"true", "false"}:
                raise _invalid(f"recorded condition branch for node '{node.id}' is not replayable")
            branch = str(raw)
        store(node, value)
        processed.add(node.id)
        record(node, "success", output=value, replayed=True)
        if branch is None:
            propagate_taken(node)
        else:
            propagate_branch(node, branch)

    def next_actionable() -> tuple[str, GraphNode] | None:
        """First actionable node in definition order: run it, or skip it."""
        for candidate in graph.nodes:
            if candidate.id in processed:
                continue
            edges_in = incoming[candidate.id]
            if resolved[candidate.id] < len(edges_in):
                continue
            if edges_in and taken_in[candidate.id] == 0:
                return ("skip", candidate)
            return ("run", candidate)
        return None

    while failure is None and waiting_node is None and waiting_input_node is None:
        if should_stop is not None and should_stop():
            # A cancel landed while this attempt was working: stop before
            # creating another call and let the attempt settle as canceled.
            canceled = True
            break
        action = next_actionable()
        if action is None:
            break
        mode, node = action
        if mode == "skip":
            processed.add(node.id)
            # A node whose only incoming edges were inactive was not selected; one
            # behind a failure is skipped because its upstream failed.
            reason = "upstream_failed" if failed_in[node.id] else "not_selected"
            record(node, "skipped", skip_reason=reason)
            propagate_skip(node, failed=reason == "upstream_failed")
            continue
        processed.add(node.id)
        started = time.monotonic()
        started_epoch = time.time()

        # Bound as defaults: the closure must carry this iteration's clock.
        def elapsed(
            started_epoch: float = started_epoch, started: float = started
        ) -> tuple[float, int]:
            return started_epoch, int((time.monotonic() - started) * 1000)

        executed += 1
        if executed > graph.max_steps:
            failure = StepOutcome(
                node_id="",
                node_type="",
                status="failed",
                save_as=None,
                error_code="WORKBUDDY_STEP_LIMIT_EXCEEDED",
                error_message=f"workflow exceeded max_steps={graph.max_steps}",
            )
            break

        if on_step_start is not None:
            on_step_start(node)

        if node.type in {"input", "output"}:
            # Both are local: an input node hands one declared input to the graph,
            # an output node renders the run's result.  Neither leaves the process,
            # so neither needs a retry or a dispatch intent.
            local_input: Mapping[str, Any] | None = None
            try:
                if node.type == "input":
                    declared = str(node.config.get("input"))
                    if declared not in inputs:
                        raise OctopError(
                            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
                            f"input node '{node.id}' names input '{declared}'"
                            " which this run did not provide",
                        )
                    value = inputs[declared]
                else:
                    value = render_template(
                        node.config.get("value"), inputs=inputs, node_results=node_results
                    )
                local_input = _activation(
                    graph, node, inputs=inputs, bindings=bindings, input_value=value
                )
            except OctopError as exc:
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=exc.code.value,
                        error_message=exc.message,
                        timing=elapsed(),
                        input=local_input,
                    )
                )
                propagate_skip(node, failed=True)
                continue
            store(node, value)
            record(node, "success", output=value, timing=elapsed(), input=local_input)
            propagate_taken(node)
            continue

        if node.type == "knowledge":
            knowledge_input = None
            if retrieve_knowledge is None:
                # Retrieval is a deployment capability (like the model and tool
                # adapters): without one the node fails closed instead of quietly
                # producing "no passages found", which would read as an empty answer.
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=ErrorCode.WORKBUDDY_DEPENDENCY_UNAVAILABLE.value,
                        error_message=("no knowledge retriever is configured for this deployment"),
                        timing=elapsed(),
                    )
                )
                propagate_skip(node, failed=True)
                continue
            try:
                rendered_query = render_template(
                    node.config.get("query"), inputs=inputs, node_results=node_results
                )
                knowledge_input = _activation(
                    graph, node, inputs=inputs, bindings=bindings, input_value=rendered_query
                )
                value = retrieve_knowledge(node, knowledge_input)
            except OctopError as exc:
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=exc.code.value,
                        error_message=exc.message,
                        timing=elapsed(),
                        input=knowledge_input,
                    )
                )
                propagate_skip(node, failed=True)
                continue
            store(node, value)
            record(node, "success", output=value, timing=elapsed(), input=knowledge_input)
            propagate_taken(node)
            continue

        if node.type in {"transform", "condition"}:
            expression = node.expression
            if expression is None:
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=ErrorCode.WORKBUDDY_VALIDATION_FAILED.value,
                        error_message=f"{node.type} node '{node.id}' has no CEL expression",
                        timing=elapsed(),
                    )
                )
                propagate_skip(node, failed=True)
                continue
            # The context the expression is resolved against is what this node was
            # given; a node whose input could not be rendered has none to record.
            node_input: Mapping[str, Any] | None = None
            try:
                rendered = (
                    render_template(
                        node.config.get("input"), inputs=inputs, node_results=node_results
                    )
                    if node.type == "transform"
                    else None
                )
                node_input = _activation(
                    graph, node, inputs=inputs, bindings=bindings, input_value=rendered
                )
                value = _evaluate(expression, node_input, node)
            except OctopError as exc:
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=exc.code.value,
                        error_message=exc.message,
                        timing=elapsed(),
                        input=node_input,
                    )
                )
                propagate_skip(node, failed=True)
                continue
            if node.type == "condition":
                if not isinstance(value, bool):
                    node_failures.append(
                        record(
                            node,
                            "failed",
                            error_code=ErrorCode.WORKBUDDY_VALIDATION_FAILED.value,
                            error_message=(
                                f"condition node '{node.id}' did not evaluate to a boolean"
                            ),
                            timing=elapsed(),
                            input=node_input,
                        )
                    )
                    # A condition that cannot choose marks both edges failed.
                    propagate_skip(node, failed=True)
                    continue
                branch = "true" if value else "false"
                selected = [edge for edge in graph.outgoing(node.id) if edge.when == branch]
                if len(selected) != 1:
                    node_failures.append(
                        record(
                            node,
                            "failed",
                            error_code=ErrorCode.WORKBUDDY_VALIDATION_FAILED.value,
                            error_message=(
                                f"condition node '{node.id}' has no unique '{branch}' branch"
                            ),
                            timing=elapsed(),
                            input=node_input,
                        )
                    )
                    propagate_skip(node, failed=True)
                    continue
                store(node, {"branch": branch})
                record(
                    node,
                    "success",
                    output={"branch": branch},
                    timing=elapsed(),
                    input=node_input,
                )
                propagate_branch(node, branch)
                continue
            store(node, value)
            record(node, "success", output=value, timing=elapsed(), input=node_input)
            propagate_taken(node)
            continue

        if node.type == "approval":
            decision = decisions.get(node.id)
            if decision not in APPROVAL_DECISIONS:
                candidates = tuple(resolve_approvers(node)) if resolve_approvers else ()
                if resolve_approvers is not None and not candidates:
                    # Nobody can decide this approval, so parking the execution
                    # would strand it forever. The node fails in place: its own
                    # downstream is skipped while independent branches keep
                    # running, and no approval request or token is ever created.
                    node_failures.append(
                        record(
                            node,
                            "failed",
                            error_code=ErrorCode.APPROVAL_NO_VALID_APPROVER.value,
                            error_message=(f"approval node '{node.id}' has no eligible approver"),
                            timing=elapsed(),
                        )
                    )
                    propagate_skip(node, failed=True)
                    continue
                if candidates:
                    approval_candidates[node.id] = candidates
                waiting_node = node.id
                record(node, "waiting_approval", timing=elapsed())
                break
            if decision == "rejected":
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code="WORKBUDDY_APPROVAL_REJECTED",
                        error_message=f"approval node '{node.id}' was rejected",
                        timing=elapsed(),
                    )
                )
                propagate_skip(node, failed=True)
                continue
            store(node, {"decision": "approved"})
            record(node, "success", output={"decision": "approved"}, timing=elapsed())
            propagate_taken(node)
            continue

        if node.type == "ask":
            if node.id in expired:
                # The deadline passed before anybody answered. Parking again would
                # ask the same question forever, so the node fails where it stands
                # and the execution settles (the sweep that marked the request has
                # already escalated it).
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=ErrorCode.INPUT_REQUEST_EXPIRED.value,
                        error_message=f"question '{node.id}' expired before it was answered",
                        timing=elapsed(),
                    )
                )
                propagate_skip(node, failed=True)
                continue
            answer = answers.get(node.id)
            if answer is None:
                assignees = tuple(resolve_assignees(node)) if resolve_assignees else ()
                if resolve_assignees is not None and not assignees:
                    # Nobody can answer, so parking would strand the execution
                    # forever: the node fails in place, its downstream is skipped
                    # while independent branches keep running, and no input
                    # request is ever created.  An approval node behaves the same
                    # way for the same reason.
                    node_failures.append(
                        record(
                            node,
                            "failed",
                            error_code=ErrorCode.ASK_NO_VALID_ASSIGNEE.value,
                            error_message=f"ask node '{node.id}' has no eligible assignee",
                            timing=elapsed(),
                        )
                    )
                    propagate_skip(node, failed=True)
                    continue
                if assignees:
                    input_assignees[node.id] = assignees
                waiting_input_node = node.id
                record(node, "waiting_input", timing=elapsed())
                break
            store(node, dict(answer))
            record(node, "success", output=dict(answer), timing=elapsed())
            propagate_taken(node)
            continue

        # External nodes: only a trusted adapter may run them; otherwise the
        # step fails closed and the execution records that failure.
        settled = replay.failed.get(node.id)
        if settled is not None:
            error_code, error_message = settled
            node_failures.append(
                record(
                    node,
                    "failed",
                    error_code=error_code,
                    error_message=error_message,
                    replayed=True,
                )
            )
            propagate_skip(node, failed=True)
            continue
        max_attempts, backoff_sec = _retry_budget(node)
        # What the registry says about this tool decides whether a *confirmed*
        # failure may be repeated; without a declaration only a failure that
        # provably dispatched nothing is retryable.
        declaration = (
            resolve_tool_declaration(node)
            if resolve_tool_declaration is not None and node.type == "tool"
            else None
        )
        dispatched = 0
        call_value: Any = None
        call_error: OctopError | None = None
        parked: UnresolvedToolOutcome | None = None
        while True:
            dispatched += 1
            call_error = None
            try:
                node_input = _activation(graph, node, inputs=inputs, bindings=bindings)
                if on_step_dispatch is not None:
                    # Durable before the call leaves the process: a worker that
                    # dies here leaves the operation key, not a mystery.
                    on_step_dispatch(node)
                if node.type == "tool":
                    call_value = port.execute_tool(
                        node=node,
                        activation=node_input,
                        # The same logical operation key on every attempt: an
                        # idempotent provider de-duplicates on it.
                        idempotency_key=f"{execution_id}:{node.id}",
                    )
                else:
                    call_value = port.execute_llm(node=node, activation=node_input)
                break
            except UnresolvedToolOutcome as outcome:
                parked = outcome
                break
            except OctopError as exc:
                call_error = exc
                # ``RETRYABLE_ERROR_CODES`` means the call never left the process;
                # a *confirmed* failure of a read-only or idempotent tool is also
                # safe to repeat, and nothing else is.
                retryable = exc.code in RETRYABLE_ERROR_CODES or (
                    declaration is not None and declaration.repeatable
                )
                if not retryable or dispatched >= max_attempts:
                    break
                if not _sleep_backoff(backoff_sec, should_stop):
                    # A cancel landed while backing off: stop instead of retrying.
                    break
        if parked is not None:
            # The write may or may not have happened. Park the step: downstream
            # stays unrunnable until an operator reconciles the evidence, and the
            # tool is never called again for this attempt.
            record(
                node,
                "waiting_reconciliation",
                output=parked.facts(),
                timing=elapsed(),
                input=node_input,
            )
            reconciliation_node = node.id
            break
        if call_error is not None:
            node_failures.append(
                record(
                    node,
                    "failed",
                    error_code=call_error.code.value,
                    error_message=call_error.message,
                    timing=elapsed(),
                    retries=dispatched - 1,
                    input=node_input,
                )
            )
            propagate_skip(node, failed=True)
            continue
        if declaration is not None and declaration.output_schema is not None:
            # A result that does not satisfy the registered schema is a contract
            # failure, not a success this run may build on.
            invalid = _schema_violation(declaration.output_schema, call_value)
            if invalid is not None:
                node_failures.append(
                    record(
                        node,
                        "failed",
                        error_code=ErrorCode.WORKBUDDY_VALIDATION_FAILED.value,
                        error_message=(
                            f"tool '{node.id}' returned a result that does not match its "
                            f"registered output schema: {invalid}"
                        ),
                        timing=elapsed(),
                        retries=dispatched - 1,
                        input=node_input,
                    )
                )
                propagate_skip(node, failed=True)
                continue
        store(node, call_value)
        record(
            node,
            "success",
            output=call_value,
            timing=elapsed(),
            tokens=_reported_tokens(call_value) if node.type == "llm" else 0,
            retries=dispatched - 1,
            input=node_input,
            # Only a model call has usage to report, and only when its adapter
            # reported one: a tool step records none rather than a zero.
            token_usage=_reported_usage(call_value) if node.type == "llm" else None,
        )
        propagate_taken(node)

    if canceled:
        # The user's cancel outranks the ordinary settlement: the attempt stops
        # where it was told to and reports canceled, whatever the branches so far
        # would otherwise have added up to.
        return GraphRun(
            status="canceled",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
        )
    if reconciliation_node is not None:
        return GraphRun(
            status="waiting_reconciliation",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
            reconciliation_node_id=reconciliation_node,
        )
    if waiting_node is not None:
        return GraphRun(
            status="waiting_approval",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
            waiting_approval_node_id=waiting_node,
            approval_candidates=approval_candidates,
        )
    if waiting_input_node is not None:
        return GraphRun(
            status="waiting_input",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
            waiting_input_node_id=waiting_input_node,
            input_assignees=input_assignees,
        )
    # Settlement follows the selected branches: a terminal that succeeded marks a
    # successful branch, so a failure elsewhere makes the execution partial rather
    # than failed. A failure with no successful terminal fails the execution, and a
    # legal DAG that produced neither is an engine invariant violation, not a
    # success.
    successful = {step.node_id for step in steps if step.status == "success"}
    terminals = [node.id for node in graph.nodes if not graph.outgoing(node.id)]
    has_success_terminal = any(node_id in successful for node_id in terminals)
    first_failure = failure or (node_failures[0] if node_failures else None)
    if first_failure is not None:
        return GraphRun(
            status="partial" if has_success_terminal else "failed",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
            error_code=first_failure.error_code,
            error_message=first_failure.error_message,
        )
    if not has_success_terminal:
        return GraphRun(
            status="failed",
            steps=tuple(steps),
            edges=tuple(edge_outcomes),
            outputs=dict(results),
            tokens=sum(step.tokens for step in steps),
            error_code="WORKBUDDY_EXECUTION_NO_TERMINAL",
            error_message="the workflow produced neither a successful terminal nor a failure",
        )
    return GraphRun(
        status="success",
        steps=tuple(steps),
        edges=tuple(edge_outcomes),
        outputs=dict(results),
        tokens=sum(step.tokens for step in steps),
    )


# ---------------------------------------------------------------------------
# Tenant-facing views (secret-free projections)
# ---------------------------------------------------------------------------


class RuntimeShadowRunner:
    """Replays a proposal's candidate against this tenant's recordings.

    The recordings are the settled step outputs of the workflow's newest
    successful production run, so a shadow run sees the same inputs the
    production run saw and cannot reach anything live.
    """

    def __init__(
        self,
        db: DatabasePool,
        tenant_id: str,
        *,
        runs: int = 10,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._runs = max(int(runs), 1)

    @property
    def _ctx(self) -> WorkBuddyDbContext:
        return WorkBuddyDbContext.for_tenant(self._tenant_id)

    def _proposal(self, proposal_id: str) -> Any:
        from octop.infra.db.repos.workbuddy_proposals import WorkBuddyProposalsRepo

        record = WorkBuddyProposalsRepo(self._db, self._ctx).get_proposal(proposal_id)
        if record is None:  # pragma: no cover - the caller resolved it already
            raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, "proposal is not visible")
        return record

    def _recordings(self, record: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        repo = WorkBuddyRuntimeRepo(self._db)
        source_id = repo.latest_succeeded_execution(self._ctx, str(record.workflow_id))
        if source_id is None:
            return {}, {}
        execution = repo.get_execution(self._ctx, source_id)
        return repo.recorded_outputs(self._ctx, source_id), dict(
            execution.inputs if execution is not None else {}
        )

    def can_replay(self, proposal_id: str) -> bool:
        record = self._proposal(proposal_id)
        recordings, _inputs = self._recordings(record)
        return bool(recordings)

    def produce(self, proposal_id: str) -> list[Any]:
        from octop.infra.workbuddy.proposals import ShadowRunRow

        record = self._proposal(proposal_id)
        recordings, inputs = self._recordings(record)
        if not recordings:
            raise OctopError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "no recorded responses are available for this workflow",
            )
        runner = ShadowRunner(WorkflowCatalogVersions(self._db))
        runs: list[Any] = []
        for index in range(self._runs):
            outcome = runner.run(
                self._ctx,
                workflow_id=str(record.workflow_id),
                candidate_version_id=str(record.candidate_version_id),
                inputs={**inputs, "shadow_run": index},
                recordings=recordings,
            )
            runs.append(
                ShadowRunRow(
                    run_id=f"{record.proposal_id}:{index}",
                    settled=bool(outcome["settled"]),
                    replay_only=bool(outcome["replay_only"]),
                    live_side_effects=int(outcome["live_side_effects"]),
                    evidence_hash=str(outcome["evidence_hash"]),
                    created_at=int(time.time()),
                )
            )
        return runs


class RuntimeCanaryMetrics:
    """Cohort metrics from the runtime's own executions, for the proposals slice.

    The tenant is fixed when the adapter is built, exactly as it is for the
    repositories, so the proposals service can ask by proposal id alone.
    """

    def __init__(self, db: DatabasePool, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    @property
    def _ctx(self) -> WorkBuddyDbContext:
        # Built on use: constructing the adapter must not require a database.
        return WorkBuddyDbContext.for_tenant(self._tenant_id)

    def settled_rows(self, proposal_id: str, *, window_start: int, window_end: int) -> list[Any]:
        from octop.infra.workbuddy.proposals import ExecutionMetricRow

        rows = WorkBuddyRuntimeRepo(self._db).canary_metrics(
            self._ctx, proposal_id, window_start=window_start, window_end=window_end
        )
        return [
            ExecutionMetricRow(
                cohort=row["cohort"],
                success=row["status"] == "success",
                active_duration_ms=row["active_duration_ms"],
                wait_ms=row["wait_ms"],
            )
            for row in rows
        ]


class ProposalCanaryDirectory:
    """Active canary routes from the proposals slice (read-only).

    The bucket comes from the proposals module's published formula, so a route
    computed here is the same one any other implementation of the contract
    computes for the same subject.
    """

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def route(self, *, tenant_id: str, workflow_id: str, subject: str) -> CanaryRoute | None:
        try:
            from octop.infra.db.repos.workbuddy_proposals import WorkBuddyProposalsRepo
            from octop.infra.db.workbuddy_context import WorkBuddyDbContext
            from octop.infra.workbuddy.proposals import (
                CANARY_BUCKET_MODULUS,
                canary_bucket,
                canary_key,
            )
        except ImportError as exc:  # pragma: no cover - proposals slice not deployed
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "the proposals slice is not available",
            ) from exc
        ctx = WorkBuddyDbContext.for_tenant(tenant_id)
        active = WorkBuddyProposalsRepo(self._db, ctx).active_canary(workflow_id)
        if active is None:
            return None
        ratio = int(active.canary_ratio_bp or 0)
        key = canary_key(tenant_id, str(active.workflow_id), subject)
        bucket = canary_bucket(key)
        candidate = bucket < ratio <= CANARY_BUCKET_MODULUS
        return CanaryRoute(
            proposal_id=str(active.proposal_id),
            cohort="canary" if candidate else "baseline",
            version_id=str(active.candidate_version_id if candidate else active.base_version_id),
            bucket=bucket,
            ratio_basis_points=ratio,
            subject=subject,
        )


@dataclass(frozen=True, slots=True)
class RuntimeActor:
    tenant_id: str
    user_id: int | None
    role: str
    tenant_status: str
    department_id: str | None = None
    # ``kind`` names the audit actor explicitly for actors that are not members
    # (the worker runs an execution on the platform's behalf).
    kind: str | None = None
    # A platform actor is not bounded by the per-user ownership rule that scopes
    # ordinary members to the work they started.
    platform: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role in TENANT_ADMIN_ROLES

    @property
    def actor_kind(self) -> str:
        return self.kind or ("admin" if self.is_admin else "member")

    @property
    def acting_user_id(self) -> int:
        """The user id a write must name; only a platform actor lacks one."""
        if self.user_id is None:
            raise OctopError(
                ErrorCode.INTERNAL_ERROR,
                "this operation needs an acting user",
            )
        return self.user_id

    @property
    def suspended(self) -> bool:
        return self.tenant_status != "active"


@dataclass(frozen=True, slots=True)
class ExecutionView:
    id: str
    workflow_id: str
    workflow_version_id: str
    status: str
    trigger_type: str
    inputs: Mapping[str, Any]
    outputs: Mapping[str, Any]
    error_code: str | None
    error_message: str | None
    created_by_user_id: int | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool = False
    active_duration_ms: int = 0
    token_usage: int = 0
    cohort: str | None = None
    proposal_id: str | None = None
    bucket: int | None = None
    route_canary_percent: int | None = None
    subject: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_version_id": self.workflow_version_id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "inputs": _json_safe(self.inputs),
            "outputs": _json_safe(self.outputs),
            "cancel_requested": self.cancel_requested,
            "active_duration_ms": self.active_duration_ms,
            "token_usage": self.token_usage,
            "cohort": self.cohort,
            "proposal_id": self.proposal_id,
            "bucket": self.bucket,
            "route_canary_percent": self.route_canary_percent,
            "subject": self.subject,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_by_user_id": self.created_by_user_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class ApprovalView:
    id: str
    execution_id: str
    node_id: str
    status: str
    required_approvals: int
    decided_approvals: int
    params: Mapping[str, Any]
    decision: str | None
    decided_at: str | None
    created_at: str | None
    token_expires_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "status": self.status,
            "required_approvals": self.required_approvals,
            "decided_approvals": self.decided_approvals,
            "params": _json_safe(self.params),
            "decision": self.decision,
            "decided_at": self.decided_at,
            "created_at": self.created_at,
            "token_expires_at": self.token_expires_at,
        }


@dataclass(frozen=True, slots=True)
class StepRunView:
    node_id: str
    node_type: str
    attempt: int
    status: str
    save_as: str | None
    error_code: str | None
    skip_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    tool_id: str | None = None
    tool_call_key: str | None = None
    dispatch_intent_at: str | None = None
    # What the node was dispatched with, its output, and the usage the call
    # reported. A fact this deployment never recorded stays ``None`` rather than
    # disappearing: an operator has to be able to tell "nothing was recorded"
    # from "the field is not part of the answer".
    input: Mapping[str, Any] | None = None
    input_sha256: str | None = None
    output: Any = None
    token_usage: Mapping[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "attempt": self.attempt,
            "status": self.status,
            "save_as": self.save_as,
            "skip_reason": self.skip_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "input": _json_safe(self.input),
            # The digest of that input, kept even when the payload itself was
            # too large for the ledger to hold.
            "input_sha256": self.input_sha256,
            "output": _json_safe(self.output),
            "token_usage": _json_safe(self.token_usage),
            "error_code": self.error_code,
            # The operation key an operator quotes when reconciling a lost answer.
            "tool_id": self.tool_id,
            "tool_call_key": self.tool_call_key,
            "dispatch_intent_at": self.dispatch_intent_at,
        }


@dataclass(frozen=True, slots=True)
class JobView:
    id: str
    kind: str
    status: str
    progress: int
    execution_id: str | None
    result: Any
    error_code: str | None
    error_message: str | None
    created_at: str | None
    finished_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "execution_id": self.execution_id,
            "result": _json_safe(self.result),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class NotificationView:
    id: str
    kind: str
    title: str
    body: str | None
    resource_type: str | None
    resource_id: str | None
    read: bool
    created_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "read": self.read,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ChatSessionView:
    id: str
    title: str
    message_count: int
    created_at: str | None
    updated_at: str | None
    messages: tuple[Mapping[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "message_count": self.message_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.messages:
            payload["messages"] = [_json_safe(message) for message in self.messages]
        return payload


@dataclass(frozen=True, slots=True)
class AuditLogView:
    id: str
    actor_user_id: int | None
    actor_kind: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    details: Mapping[str, Any]
    created_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor_user_id": self.actor_user_id,
            "actor_kind": self.actor_kind,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "outcome": self.outcome,
            "details": _json_safe(self.details),
            "created_at": self.created_at,
        }


def _reported_usage(value: Any) -> dict[str, Any] | None:
    """The usage object a model adapter reported, or None when it reported none.

    Kept in the shape the adapter sent, because the platform's model adapters
    name their own counters; nothing is added for a count nobody reported.
    """
    usage = value.get("usage") if isinstance(value, Mapping) else None
    if not isinstance(usage, Mapping) or not usage:
        return None
    return dict(_json_safe(usage))


def _reported_tokens(value: Any) -> int:
    """Tokens a model adapter reported, in the shape the platform's chat uses."""
    usage = value.get("usage") if isinstance(value, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0
    total = usage.get("total_tokens")
    if total is None:
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = prompt + completion if (prompt or completion) else 0
    try:
        return max(int(total), 0)
    except (TypeError, ValueError):
        return 0


def _latest_attempts(steps: Sequence[Any]) -> dict[str, Any]:
    """Newest attempt per node: an earlier attempt must not describe the present."""
    latest: dict[str, Any] = {}
    for step in sorted(steps, key=lambda item: int(getattr(item, "attempt", 1))):
        latest[step.node_id] = step
    return latest


def validate_correction(
    produced: Mapping[str, Any], corrected: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A correction may replace the values a run produced, and nothing else.

    Adding a key would let a reviewer invent a result the workflow never produced,
    and a later consumer could not tell that value apart from real output.  Keys
    may be left out (a correction says what to change), so the result is always a
    subset of what the run actually produced.
    """
    if not isinstance(corrected, Mapping) or not corrected:
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
            "a correction needs the output values it replaces",
        )
    unknown = sorted(str(key) for key in corrected if str(key) not in produced)
    if unknown:
        listed = ", ".join(repr(key) for key in unknown)
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
            f"the run produced no output named {listed}",
        )
    return {str(key): value for key, value in corrected.items()}


def output_review_payload(
    row: OutputReviewRow, reviewers: Sequence[OutputReviewReviewerRow] = ()
) -> dict[str, Any]:
    """One output review as the API and the reviewing UI need it.

    ``produced`` and ``corrected`` travel together on purpose: a reviewer decides
    by comparing them, and the run's own payloads stay the source of truth for what
    the workflow actually produced.
    """
    return {
        "id": row.id,
        "execution_id": row.execution_id,
        "status": row.status,
        "produced": dict(row.produced) if isinstance(row.produced, Mapping) else {},
        "produced_sha256": row.produced_sha256,
        "corrected": dict(row.corrected) if isinstance(row.corrected, Mapping) else None,
        "requested_by_user_id": row.requested_by_user_id,
        "decided_by_user_id": row.decided_by_user_id,
        "decided_at": _iso(row.decided_at),
        "rerun_execution_id": row.rerun_execution_id,
        "created_at": _iso(row.created_at),
        "reviewers": [
            {
                "user_id": reviewer.user_id,
                "department_id": reviewer.department_id,
                "status": reviewer.status,
                "decided_at": _iso(reviewer.decided_at),
            }
            for reviewer in reviewers
        ],
    }


def feedback_payload(row: ExecutionFeedbackRow) -> dict[str, Any]:
    """One feedback record as the API needs it.

    ``before`` and ``after`` travel together because a correction is only readable
    as a pair: the value the workflow produced and the value a person said it
    should have been.
    """
    return {
        "id": row.id,
        "execution_id": row.execution_id,
        "workflow_id": row.workflow_id,
        "workflow_version_id": row.workflow_version_id,
        "node_id": row.node_id,
        "output_key": row.output_key,
        "kind": row.kind,
        "source": row.source,
        "source_id": row.source_id,
        "before": row.before,
        "after": row.after,
        "created_by_user_id": row.created_by_user_id,
        "created_at": _iso(row.created_at),
    }


def input_request_payload(
    row: InputRequestRow, assignees: Sequence[InputAssigneeRow] = ()
) -> dict[str, Any]:
    """One input request as the API and the answering UI need it.

    The form travels with the answer, because a client that shows a question has
    to render the fields it will validate against; ``values`` stays null until
    somebody answers, so the UI can tell an open question from an answered one
    without a second request.
    """
    return {
        "id": row.id,
        "execution_id": row.execution_id,
        "node_id": row.node_id,
        "status": row.status,
        "prompt": row.prompt,
        "form": dict(row.form) if isinstance(row.form, Mapping) else {},
        "values": dict(row.values) if isinstance(row.values, Mapping) else None,
        "expires_at": _iso(row.expires_at),
        "submitted_by_user_id": row.submitted_by_user_id,
        "submitted_at": _iso(row.submitted_at),
        "created_at": _iso(row.created_at),
        "assignees": [
            {
                "user_id": assignee.user_id,
                "department_id": assignee.department_id,
                "status": assignee.status,
                "submitted_at": _iso(assignee.submitted_at),
            }
            for assignee in assignees
        ],
    }


def execution_wait_facts(view: ExecutionView, steps: Sequence[Any]) -> dict[str, Any]:
    """Derived waiting facts for the execution detail (never stored separately).

    Only the newest attempt of a node describes the present, and two kinds of
    waiting can coexist: reconciliation is reported first because an unknown
    external write is the more serious of the two.
    """
    latest = _latest_attempts(steps)
    waiting = [step for step in latest.values() if step.status == "waiting_reconciliation"]
    approval = [step for step in latest.values() if step.status == "waiting_approval"]
    answer = [step for step in latest.values() if step.status == "waiting_input"]
    reasons: list[str] = []
    if waiting:
        reasons.append("reconciliation")
    if approval:
        reasons.append("approval")
    if answer:
        reasons.append("input")
    return {
        "wait_reasons": reasons,
        "waiting_steps": [step.node_id for step in (*waiting, *approval, *answer)],
        "cancel_requested": view.cancel_requested,
    }


def _execution_view(row: ExecutionRow) -> ExecutionView:
    return ExecutionView(
        id=row.id,
        workflow_id=row.workflow_id,
        workflow_version_id=row.workflow_version_id,
        status=row.status,
        trigger_type=row.trigger_type,
        inputs=row.inputs,
        outputs=row.outputs,
        error_code=row.error_code,
        error_message=row.error_message,
        created_by_user_id=row.created_by_user_id,
        created_at=_iso(row.created_at),
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
        cancel_requested=row.cancel_requested_at is not None,
        active_duration_ms=row.active_duration_ms,
        token_usage=row.token_usage,
        cohort=row.cohort,
        proposal_id=row.proposal_id,
        bucket=row.bucket,
        route_canary_percent=row.route_canary_percent,
        subject=row.subject,
    )


def _approval_view(row: ApprovalRequestRow) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        execution_id=row.execution_id,
        node_id=row.node_id,
        status=row.status,
        required_approvals=row.required_approvals,
        decided_approvals=row.decided_approvals,
        params=row.params if isinstance(row.params, Mapping) else {},
        decision=row.decision,
        decided_at=_iso(row.decided_at),
        created_at=_iso(row.created_at),
        token_expires_at=_iso(row.token_expires_at),
    )


def _job_view(row: JobRow) -> JobView:
    return JobView(
        id=row.id,
        kind=row.kind,
        status=row.status,
        progress=row.progress,
        execution_id=row.execution_id,
        result=row.result,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=_iso(row.created_at),
        finished_at=_iso(row.finished_at),
    )


def _notification_view(row: NotificationRow) -> NotificationView:
    return NotificationView(
        id=row.id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        read=row.read_at is not None,
        created_at=_iso(row.created_at),
    )


def _audit_view(row: AuditLogRow) -> AuditLogView:
    return AuditLogView(
        id=row.id,
        actor_user_id=row.actor_user_id,
        actor_kind=row.actor_kind,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        outcome=row.outcome,
        details=row.details if isinstance(row.details, Mapping) else {},
        created_at=_iso(row.created_at),
    )


def _chat_session_view(
    row: ChatSessionRow, messages: Sequence[ChatMessageRow] = ()
) -> ChatSessionView:
    return ChatSessionView(
        id=row.id,
        title=row.title,
        message_count=row.message_count,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        messages=tuple(
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "created_at": _iso(message.created_at),
            }
            for message in messages
        ),
    )


def approval_requirements(node: GraphNode) -> tuple[tuple[str, ...], int]:
    """Approver membership ids and decision timeout hours from the node config.

    The workflow schema requires ``approver_user_ids`` (membership ids) and
    ``approval_message``; everyone listed must approve, so the required approval
    count is the number of eligible, still-active candidates.
    """
    raw = node.config.get("approver_user_ids")
    approvers = tuple(
        str(value)
        for value in (raw if isinstance(raw, Sequence) else [])
        if isinstance(value, str) and value
    )
    timeout = node.config.get("timeout_hours")
    timeout_hours = int(timeout) if isinstance(timeout, int) and 1 <= timeout <= 168 else 24
    return approvers, timeout_hours


def ask_requirements(node: GraphNode) -> tuple[tuple[str, ...], int]:
    """Assignee membership ids and answer timeout hours from an ask node.

    The workflow schema requires ``assignee_user_ids`` (membership ids), a
    ``prompt`` and at least one field; the timeout is what bounds the wait, so a
    run parked on a form does not wait forever by default (24 hours, the same
    default an approval uses).
    """
    raw = node.config.get("assignee_user_ids")
    assignees = tuple(
        str(value)
        for value in (raw if isinstance(raw, Sequence) else [])
        if isinstance(value, str) and value
    )
    timeout = node.config.get("timeout_hours")
    timeout_hours = int(timeout) if isinstance(timeout, int) and 1 <= timeout <= 168 else 24
    return assignees, timeout_hours


def _answer_refused(field_name: str, reason: str) -> OctopError:
    return OctopError(
        ErrorCode.WORKBUDDY_VALIDATION_FAILED,
        f"answer for field {field_name!r} {reason}",
    )


def _coerce_answer(field_name: str, spec: Mapping[str, Any], value: Any) -> Any:
    """One submitted value, in the shape the declared field type promises."""
    kind = str(spec.get("type") or "")
    if kind not in ASK_FIELD_TYPES:
        # A type the schema does not define can only come from a hand-edited row
        # or a schema newer than this runtime, and accepting any value for it
        # would store something no reader agrees on: refuse the answer instead.
        raise _answer_refused(field_name, f"declares an unsupported type {kind!r}")
    if kind in {"string", "text", "date"}:
        if not isinstance(value, str):
            raise _answer_refused(field_name, "must be text")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _answer_refused(field_name, "must be a whole number")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _answer_refused(field_name, "must be a number")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _answer_refused(field_name, "must be true or false")
        return value
    options = [str(option) for option in spec.get("options") or ()]
    if not isinstance(value, str) or value not in options:
        raise _answer_refused(field_name, f"must be one of {options}")
    return value


def validate_answers(form: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """Check one submitted answer against the form the run was asked with.

    The answer becomes a step's output, so what the graph reads must be exactly
    what the form declared: an undeclared key would let a caller invent data for
    a question that was never asked, and a missing required field would store an
    absence the workflow would go on to treat as a value.
    """
    raw_fields = form.get("fields") if isinstance(form, Mapping) else None
    declared: dict[str, Mapping[str, Any]] = {}
    for spec in raw_fields if isinstance(raw_fields, Sequence) else ():
        if isinstance(spec, Mapping) and isinstance(spec.get("name"), str):
            declared[str(spec["name"])] = spec
    if not declared:
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
            "the input request carries no usable form",
        )
    unknown = sorted(str(key) for key in values if str(key) not in declared)
    if unknown:
        listed = ", ".join(repr(key) for key in unknown)
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED,
            f"the form does not declare {listed}",
        )
    cleaned: dict[str, Any] = {}
    for name, spec in declared.items():
        present = name in values
        value = values.get(name)
        blank = value is None or (isinstance(value, str) and not value.strip())
        if not present or blank:
            if bool(spec.get("required", True)):
                raise _answer_refused(name, "is required")
            continue
        cleaned[name] = _coerce_answer(name, spec, value)
    return cleaned


class MembershipApproverResolver:
    """Resolve approver membership ids to active tenant members (identity slice)."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def __call__(
        self, tenant_id: str, member_ids: Sequence[str]
    ) -> Sequence[tuple[int, str | None]]:
        from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

        repo = WorkBuddyIdentityRepo(self._db)
        resolved: list[tuple[int, str | None]] = []
        for member_id in member_ids:
            member = repo.get_member(tenant_id, member_id)
            if member is None or str(member.get("status") or "") != "active":
                continue
            if bool(member.get("disabled")):
                continue
            resolved.append((int(member["user_id"]), member.get("department_id")))
        return resolved


@dataclass(frozen=True, slots=True)
class RuntimeJobRecorder:
    """Record another slice's asynchronous operation in the tenant's job facts.

    Contract §4.6.2 keeps jobs first-class: a client that lost a response can find
    what it started with ``GET /jobs``. Slices whose work is a job (proposal
    generation, document indexing, install, export) record it here instead of
    borrowing their resource id as a job id.
    """

    db: DatabasePool
    tenant_id: str
    user_id: int | None = None

    def start(self, *, kind: str, request: Mapping[str, Any] | None = None) -> str:
        ctx = WorkBuddyDbContext.for_tenant(self.tenant_id, user_id=self.user_id)
        return WorkBuddyRuntimeRepo(self.db).insert_job(
            ctx,
            tenant_id=self.tenant_id,
            kind=kind,
            requested_by_user_id=self.user_id,
            request_hash=_hash_json(dict(request or {}))[0],
        )

    def begin(self, job_id: str) -> bool:
        """Mark the job as running while the work actually happens."""
        ctx = WorkBuddyDbContext.for_tenant(self.tenant_id, user_id=self.user_id)
        return WorkBuddyRuntimeRepo(self.db).start_job(ctx, job_id)

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        ctx = WorkBuddyDbContext.for_tenant(self.tenant_id, user_id=self.user_id)
        return WorkBuddyRuntimeRepo(self.db).finish_job(
            ctx,
            job_id,
            status=status,
            progress=100 if status == "succeeded" else 0,
            result=dict(result) if result is not None else None,
            error_code=error_code,
            error_message=error_message,
        )


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """What the platform registry says about the tool a node calls.

    The engine reads it before dispatching: a read-only tool may be repeated, an
    external write only when its idempotency semantics are confirmed (contract
    line 891), and a declared result schema must hold for a success to count.
    """

    effect_class: str
    supports_idempotency: bool
    output_schema: Mapping[str, Any] | None = None

    @property
    def repeatable(self) -> bool:
        """Whether a *confirmed* failure may be followed by another call."""
        return self.effect_class == "read_only" or self.supports_idempotency


class CatalogToolDeclarations:
    """Resolve a node's declared tool to the tenant's granted platform revision.

    Resolution is per tenant on purpose: a tool the tenant has no grant for is
    undeclared, which is the conservative answer (no retry beyond a failure that
    provably dispatched nothing, and no registered result schema).
    """

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def __call__(self, tenant_id: str, node: GraphNode) -> ToolDeclaration | None:
        tool_key = node.config.get("tool_name") if isinstance(node.config, Mapping) else None
        if not isinstance(tool_key, str) or not tool_key:
            return None
        ctx = WorkBuddyDbContext.for_tenant(tenant_id)
        with runtime_transaction(self._db, ctx) as conn:
            row = conn.execute(
                """
                SELECT r.effect_class, r.supports_idempotency, r.output_schema
                FROM workbuddy_tenant_tool_grants g
                JOIN workbuddy_platform_tool_revisions r
                  ON r.tool_revision_id = g.tool_revision_id
                WHERE r.tool_key = ? AND r.status = 'published'
                ORDER BY r.revision DESC
                LIMIT 1
                """,
                (tool_key,),
            ).fetchone()
        if row is None:
            return None
        schema = row["output_schema"]
        return ToolDeclaration(
            effect_class=str(row["effect_class"]),
            supports_idempotency=bool(row["supports_idempotency"]),
            output_schema=dict(schema) if isinstance(schema, Mapping) else None,
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WorkBuddyRuntimeService:
    """Tenant runtime operations backed by PostgreSQL execution facts."""

    def __init__(
        self,
        db: DatabasePool,
        *,
        repo: WorkBuddyRuntimeRepo | None = None,
        versions: WorkflowVersionSource | None = None,
        effects: SideEffectPort | None = None,
        member_resolver: Any | None = None,
        canary: CanaryDirectory | None = None,
        tool_declarations: Any | None = None,
        feedback: ExecutionFeedbackRepo | None = None,
    ) -> None:
        self._db = db
        self._repo = repo or WorkBuddyRuntimeRepo(db)
        # The improvement loop's raw material: what a person changed, and what the
        # workflow version was when they changed it (A-12).
        self._feedback = feedback or ExecutionFeedbackRepo(db)
        self._versions = versions or WorkflowCatalogVersions(db)
        self._effects = effects or UNAVAILABLE_SIDE_EFFECTS
        self._member_resolver = (
            member_resolver if member_resolver is not None else MembershipApproverResolver(db)
        )
        self._canary = canary or NO_CANARY_EVALUATION
        self._tool_declarations = (
            tool_declarations if tool_declarations is not None else CatalogToolDeclarations(db)
        )

    @classmethod
    def for_control_plane(cls, db: DatabasePool) -> WorkBuddyRuntimeService:
        """The service as a deployment wires it: PostgreSQL facts plus canary routing.

        Side-effect adapters stay fail-closed (``UNAVAILABLE_SIDE_EFFECTS``) until
        the platform tool catalogue can resolve a published revision; a
        deployment that has one injects ``effects=``. The API router and the
        execution worker both build the service this way, so a run means the same
        thing whichever tier evaluates it.
        """
        return cls(db, canary=ProposalCanaryDirectory(db))

    # -- helpers ------------------------------------------------------------

    def _require_postgres(self) -> None:
        require_postgres(self._db)
        if self._db.dialect != "postgresql":  # pragma: no cover - defensive
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "WorkBuddy tenant workflows require PostgreSQL; SQLite fails closed",
            )

    def _ctx(self, actor: RuntimeActor) -> WorkBuddyDbContext:
        return WorkBuddyDbContext.for_tenant(
            actor.tenant_id, user_id=actor.user_id, department_id=actor.department_id
        )

    def _assert_tenant_active(self, actor: RuntimeActor) -> None:
        """Block new starts for a suspended tenant, using the fact store as truth.

        Settlement paths (cancel, resume, reconciliation, job completion, quota
        release) deliberately do not call this: a suspended tenant must still be
        able to settle work that is already in flight.
        """
        status = self._repo.tenant_status(actor.tenant_id)
        if status is not None and status != "active":
            raise OctopError(
                ErrorCode.TENANT_SUSPENDED,
                "tenant is suspended: new starts are blocked",
            )
        if actor.suspended:
            raise OctopError(
                ErrorCode.TENANT_SUSPENDED,
                "tenant is suspended: new starts are blocked",
            )

    def _graph_from_snapshot(self, execution: ExecutionRow) -> WorkflowGraph:
        return compile_locked_definition(
            execution.definition_snapshot,
            execution.workflow_version_hash,
            version_id=execution.workflow_version_id,
        )

    def _load_execution(
        self, actor: RuntimeActor, execution_id: str, *, conn: Any = None
    ) -> ExecutionRow:
        row = self._repo.get_execution(self._ctx(actor), execution_id, conn=conn)
        if row is None or (
            not actor.is_admin and not actor.platform and row.created_by_user_id != actor.user_id
        ):
            raise _not_found()
        return row

    def _load_approval(
        self, actor: RuntimeActor, approval_request_id: str, *, conn: Any = None
    ) -> ApprovalRequestRow:
        row = self._repo.get_approval_request(self._ctx(actor), approval_request_id, conn=conn)
        if row is None:
            raise _not_found()
        candidates = self._repo.list_approval_candidates(
            self._ctx(actor), approval_request_id, conn=conn
        )
        if not actor.is_admin and all(
            candidate.user_id != actor.user_id for candidate in candidates
        ):
            raise _not_found()
        return row

    def _load_input_request(
        self, actor: RuntimeActor, input_request_id: str, *, conn: Any = None
    ) -> InputRequestRow:
        """One input request the actor may read, or a 404.

        The 404 matches the approval path on purpose: somebody who is neither an
        assignee nor an admin learns nothing about whether the request exists.
        """
        row = self._repo.get_input_request(self._ctx(actor), input_request_id, conn=conn)
        if row is None:
            raise _not_found()
        assignees = self._repo.list_input_assignees(self._ctx(actor), input_request_id, conn=conn)
        if not actor.is_admin and all(assignee.user_id != actor.user_id for assignee in assignees):
            raise _not_found()
        return row

    def _resolved_candidates(
        self, actor: RuntimeActor, node: GraphNode
    ) -> list[tuple[int, str | None]]:
        """Eligible approvers for one approval node, or a fail-closed refusal.

        Zero eligible approvers (unknown membership, suspended member, or no
        resolver wired in this deployment) is a hard failure: an execution must
        never wait forever on an approval nobody can decide.
        """
        approvers, _timeout = approval_requirements(node)
        if not approvers:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"approval node '{node.id}' has no configured approvers",
            )
        if self._member_resolver is None:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "approver resolution is not configured for this deployment",
            )
        resolved = {
            int(user_id): department_id
            for user_id, department_id in self._member_resolver(actor.tenant_id, approvers)
        }
        # An empty resolution is not an error here: the caller decides between a
        # node-scoped failure (nobody valid) and parking the execution.
        return sorted(resolved.items())

    def _resolved_assignees(
        self, actor: RuntimeActor, node: GraphNode
    ) -> list[tuple[int, str | None]]:
        """Eligible assignees for one ask node, or a fail-closed refusal.

        Same rule as approvers: a form nobody can fill would park the execution
        forever, so an empty resolution lets the caller fail the node in place.
        The membership resolver is shared with approvals — it answers "which of
        these ids is a current, acting member of this tenant", which is the
        question both node types ask.
        """
        declared, _timeout = ask_requirements(node)
        if not declared:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"ask node '{node.id}' has no configured assignees",
            )
        if self._member_resolver is None:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "assignee resolution is not configured for this deployment",
            )
        resolved = {
            int(user_id): department_id
            for user_id, department_id in self._member_resolver(actor.tenant_id, declared)
        }
        return sorted(resolved.items())

    def _audit(
        self,
        actor: RuntimeActor,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str = "allowed",
        details: Mapping[str, Any] | None = None,
        conn: Any = None,
    ) -> None:
        self._repo.append_audit(
            self._ctx(actor),
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            actor_kind=actor.actor_kind,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=dict(details or {}),
            conn=conn,
        )

    def _enforce_quota(
        self,
        actor: RuntimeActor,
        *,
        quota_key: str,
        amount: int,
        limits: Mapping[str, int] | None,
        conn: Any,
        include_usage: bool = True,
        since: Any = None,
        count_key: str | None = None,
    ) -> None:
        """Refuse the request when it would take the tenant past a limit.

        ``include_usage`` distinguishes the two shapes the contract names: a
        monthly allowance counts what has been consumed plus what is in flight,
        while a concurrency ceiling counts only the reservations that are still
        live, which is what a parked run holds until it settles. ``count_key``
        lets the ceiling read the reservations a run already made under another
        metric, so one reservation serves both limits.
        """
        if not limits:
            return
        limit = limits.get(quota_key)
        if not isinstance(limit, int):
            return
        counted = count_key or quota_key
        ctx = self._ctx(actor)
        used = (
            self._repo.quota_usage_total(
                ctx,
                tenant_id=actor.tenant_id,
                quota_key=counted,
                since=since,
                conn=conn,
            )
            if include_usage
            else 0
        )
        reserved = sum(
            item.amount
            for item in self._repo.list_live_quota_reservations(
                ctx, tenant_id=actor.tenant_id, quota_key=counted, conn=conn
            )
        )
        if used + reserved + amount > limit:
            raise OctopError(
                ErrorCode.QUOTA_EXCEEDED,
                f"tenant quota '{quota_key}' would be exceeded",
            )

    def _tenant_quota_limits(self, tenant_id: str) -> dict[str, int]:
        """The tenant's own quota rows, so a caller cannot opt out by omission."""
        from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

        limits: dict[str, int] = {}
        for row in WorkBuddyIdentityRepo(self._db).get_quotas(tenant_id):
            metric = str(row.get("metric") or "")
            limit = row.get("limit")
            if metric and isinstance(limit, int):
                limits[metric] = int(limit)
        return limits

    def _settle_execution_quota(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        conn: Any,
        consume: bool,
    ) -> None:
        ctx = self._ctx(actor)
        settled = 0
        for reservation in self._repo.list_live_quota_reservations(
            ctx, tenant_id=actor.tenant_id, execution_id=execution_id, conn=conn
        ):
            self._repo.settle_quota_reservation(
                ctx,
                reservation.id,
                status="committed"
                if consume and reservation.quota_key == "executions"
                else "released",
                conn=conn,
            )
            settled += 1
        # One consumption per execution: it is recorded when the reservation that
        # carried it is settled, never a second time from a later terminal path.
        if consume and settled:
            self._repo.record_quota_usage(
                ctx,
                tenant_id=actor.tenant_id,
                quota_key="executions",
                amount=1,
                direction="consume",
                scope="execution",
                execution_id=execution_id,
                conn=conn,
            )

    def _release_concurrency_slot(
        self, actor: RuntimeActor, execution_id: str, *, conn: Any
    ) -> None:
        ctx = self._ctx(actor)
        for reservation in self._repo.list_live_quota_reservations(
            ctx,
            tenant_id=actor.tenant_id,
            quota_key="concurrency",
            execution_id=execution_id,
            conn=conn,
        ):
            self._repo.settle_quota_reservation(ctx, reservation.id, status="released", conn=conn)

    def _persist_run(
        self,
        actor: RuntimeActor,
        execution: ExecutionRow,
        run: GraphRun,
        graph: WorkflowGraph,
        *,
        conn: Any,
    ) -> None:
        """Commit the attempt's edges and outputs.

        Step rows are not written here: each step was persisted the moment it
        settled (``_persist_step``), which is what makes a step observable while
        the run is still going.
        """
        ctx = self._ctx(actor)
        outputs_sha, outputs_size = _hash_json(run.outputs)
        if outputs_size > graph.max_output_bytes:
            raise _invalid("workflow output exceeds the definition output limit")
        for edge in run.edges:
            self._repo.insert_edge_run(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution.id,
                edge_from=edge.source,
                edge_to=edge.target,
                taken=edge.taken,
                branch=edge.branch,
                conn=conn,
            )
        outputs_sha, outputs_size = _hash_json(run.outputs)
        self._repo.insert_payload(
            ctx,
            tenant_id=actor.tenant_id,
            execution_id=execution.id,
            kind="outputs",
            content=run.outputs,
            sha256=outputs_sha,
            size_bytes=outputs_size,
            conn=conn,
        )

    # -- executions ---------------------------------------------------------

    def _existing_for_key(
        self,
        actor: RuntimeActor,
        *,
        scope: str | None,
        key: str | None,
        request_hash: str | None,
        conn: Any = None,
    ) -> ExecutionRow | None:
        """Return the recorded execution for an idempotency key or raise on mismatch."""
        if not key or scope is None:
            return None
        existing = self._repo.get_execution_by_idempotency(
            self._ctx(actor), tenant_id=actor.tenant_id, scope=scope, key=key, conn=conn
        )
        if existing is None:
            return None
        if existing.idempotency_hash != request_hash:
            raise OctopError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was already used with a different request",
            )
        return existing

    def start_execution(
        self,
        actor: RuntimeActor,
        *,
        workflow_id: str,
        inputs: Mapping[str, Any] | None = None,
        trigger_type: str = "api",
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        quota_limits: Mapping[str, int] | None = None,
        subject: str | None = None,
    ) -> ExecutionView:
        """Accept one execution: suspension gate, idempotency, monthly allowance.

        Acceptance runs nothing. The execution is recorded ``queued`` and a
        worker admits it afterwards, which is where the running-slot ceiling is
        applied (contract §8.1: the API tier holds no in-process execution state,
        and a waiting execution holds no slot).
        """
        self._require_postgres()
        self._assert_tenant_active(actor)
        if quota_limits is None:
            quota_limits = self._tenant_quota_limits(actor.tenant_id)
        payload = dict(inputs or {})
        payload_sha, payload_size = _hash_json(payload)
        if payload_size > MAX_WORKFLOW_INPUT_BYTES:
            raise _invalid("workflow inputs exceed the maximum accepted size")

        ctx = self._ctx(actor)
        # An active canary decides which version this execution runs; the bucket
        # is deterministic, so the same subject always lands in the same cohort.
        route = (
            self._canary.route(tenant_id=actor.tenant_id, workflow_id=workflow_id, subject=subject)
            if subject
            else None
        )
        locked = (
            self._versions.load_version(ctx, workflow_id, route.version_id)
            if route is not None
            else self._versions.load_active(ctx, workflow_id)
        )
        # The definition was compiled when it was published and the execution
        # locks its hash, so acceptance only has to reject a payload the declared
        # inputs cannot accept; the worker compiles the locked snapshot again.
        payload = _validate_execution_inputs(locked.definition, payload)
        payload_sha, payload_size = _hash_json(payload)
        idempotency_hash = (
            _hash_json(
                {
                    "workflow_id": workflow_id,
                    "workflow_version_id": locked.version_id,
                    "inputs": payload,
                }
            )[0]
            if idempotency_key
            else None
        )
        execution_id = new_runtime_id()
        with runtime_transaction(self._db, ctx) as conn:
            self._repo.lock_tenant_quota(ctx, tenant_id=actor.tenant_id, conn=conn)
            existing = self._existing_for_key(
                actor,
                scope=idempotency_scope,
                key=idempotency_key,
                request_hash=idempotency_hash,
                conn=conn,
            )
            if existing is not None:
                return _execution_view(existing)
            self._enforce_quota(
                actor,
                quota_key="executions",
                amount=1,
                limits=quota_limits,
                conn=conn,
                since=_period_start(),
            )
            self._repo.reserve_quota(
                ctx,
                tenant_id=actor.tenant_id,
                quota_key="executions",
                amount=1,
                scope="execution",
                ttl_seconds=EXECUTION_RESERVATION_TTL_SECONDS,
                execution_id=execution_id,
                conn=conn,
            )
            if not self._repo.insert_execution_if_absent(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution_id,
                workflow_id=workflow_id,
                workflow_version_id=locked.version_id,
                workflow_version_hash=locked.definition_sha256,
                definition_snapshot=dict(locked.definition),
                trigger_type=trigger_type,
                inputs=payload,
                created_by_user_id=actor.acting_user_id,
                # ``workbuddy_executions`` requires the scope and the key to be
                # present together, so a run without a key carries neither.
                idempotency_scope=idempotency_scope if idempotency_key else None,
                idempotency_key=idempotency_key,
                idempotency_hash=idempotency_hash,
                proposal_id=route.proposal_id if route is not None else None,
                cohort="production" if route is None else route.cohort,
                bucket=route.bucket if route is not None else None,
                route_canary_percent=(route.ratio_basis_points if route is not None else None),
                subject=subject,
                conn=conn,
            ):
                concurrent = self._existing_for_key(
                    actor,
                    scope=idempotency_scope,
                    key=idempotency_key,
                    request_hash=idempotency_hash,
                    conn=conn,
                )
                if concurrent is None:
                    raise _not_found()
                return _execution_view(concurrent)
            self._repo.insert_payload(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution_id,
                kind="inputs",
                content=payload,
                sha256=payload_sha,
                size_bytes=payload_size,
                conn=conn,
            )
            self._repo.enqueue_outbox(
                ctx,
                tenant_id=actor.tenant_id,
                topic="workbuddy.execution.started",
                dedupe_key=f"{execution_id}:started",
                payload={"execution_id": execution_id, "workflow_id": workflow_id},
                conn=conn,
            )
            self._audit(
                actor,
                action="execution.start",
                resource_type="execution",
                resource_id=execution_id,
                details={"workflow_id": workflow_id, "trigger_type": trigger_type},
                conn=conn,
            )
            accepted = self._repo.get_execution(ctx, execution_id, conn=conn)
        if accepted is None:  # pragma: no cover - defensive
            raise _not_found()
        return _execution_view(accepted)

    def _replay_state(self, ctx: WorkBuddyDbContext, execution_id: str) -> ReplayState:
        steps = self._repo.list_step_runs(ctx, execution_id)
        latest = _latest_attempts(steps)
        return ReplayState(
            outputs={
                node_id: step.output for node_id, step in latest.items() if step.status == "success"
            },
            skipped=frozenset(
                node_id for node_id, step in latest.items() if step.status == "skipped"
            ),
            failed={
                node_id: (step.error_code, step.error_message)
                for node_id, step in latest.items()
                if step.status == "failed"
            },
        )

    def run_claimed_execution(self, claim: ExecutionClaim) -> None:
        """Run one execution a worker admitted under ``claim``.

        The claim already took the running slot and the lease and moved the
        execution to ``running``, so this evaluates the locked definition,
        commits the attempt under the claim's fence, and hands the slot and the
        lease back -- whether the run finished, parked for a human, or could not
        be interpreted at all.
        """
        ctx = WorkBuddyDbContext.for_tenant(claim.tenant_id)
        execution = self._repo.get_execution(ctx, claim.execution_id)
        lease_name = execution_lease_name(claim.execution_id)
        if execution is None or execution.is_terminal:
            self._close_claim(claim, lease_name)
            return
        actor = RuntimeActor(
            tenant_id=claim.tenant_id,
            user_id=execution.created_by_user_id,
            role="member",
            tenant_status="active",
            # The platform runs what the workflow's definition declares; the
            # audit trail records the worker, not a borrowed user identity.
            kind="system",
            platform=True,
        )
        try:
            graph = self._graph_from_snapshot(execution)
        except WorkflowCompileError as exc:
            # A locked definition the engine cannot interpret is an invariant
            # failure with a root cause, never a run that silently never lands.
            self._fail_claimed_execution(actor, execution, claim, lease_name, exc)
            return
        previous_steps = self._repo.list_step_runs(ctx, claim.execution_id)
        attempt = max((step.attempt for step in previous_steps), default=0) + 1
        # Record what this attempt has not decided yet before it can decide any
        # of it, so a step is observable as waiting, running and settled.
        self._repo.queue_step_runs(
            ctx,
            tenant_id=claim.tenant_id,
            execution_id=claim.execution_id,
            nodes=tuple((node.id, node.type) for node in graph.nodes),
            attempt=attempt,
            fence=claim.fence,
        )
        run = run_graph(
            graph,
            inputs=execution.inputs,
            replay=self._replay_state(ctx, claim.execution_id),
            decisions=self._recorded_decisions(ctx, execution),
            answers=self._recorded_answers(ctx, execution),
            expired_questions=self._recorded_expired(ctx, execution),
            effects=self._effects,
            execution_id=claim.execution_id,
            resolve_approvers=lambda node: self._resolved_candidates(actor, node),
            resolve_assignees=lambda node: self._resolved_assignees(actor, node),
            on_step_start=lambda node: self._mark_step_running(actor, claim, node, attempt),
            on_step_settled=lambda step: self._persist_step(actor, claim, step, attempt),
            # A cancel is noticed between nodes, so the call already in flight
            # lands and nothing new is dispatched.
            should_stop=lambda: self._repo.cancel_requested(ctx, claim.execution_id),
            on_step_dispatch=lambda node: self._mark_step_dispatch(actor, claim, node, attempt),
            resolve_tool_declaration=lambda node: self._tool_declarations(claim.tenant_id, node),
        )
        if run.status == "canceled":
            self._repo.cancel_pending_steps(
                ctx,
                claim.execution_id,
                attempt=attempt,
                fence=claim.fence,
            )
        self._finalize(actor, execution, run, graph, claim=claim, lease_name=lease_name)

    def _mark_step_running(
        self, actor: RuntimeActor, claim: ExecutionClaim, node: GraphNode, attempt: int
    ) -> None:
        """A node just began: the step is ``running`` under this attempt's fence."""
        self._repo.mark_step_running(
            self._ctx(actor),
            execution_id=claim.execution_id,
            node_id=node.id,
            attempt=attempt,
            fence=claim.fence,
        )

    def _mark_step_dispatch(
        self, actor: RuntimeActor, claim: ExecutionClaim, node: GraphNode, attempt: int
    ) -> None:
        """Persist the operation key before the call can leave the process.

        ``tool_id`` is the tool (or model) the definition declared; a deployment
        resolves its published revision from that. The call key is the stable
        logical operation key every attempt of this node reuses, which is what an
        idempotent provider de-duplicates on and what an operator quotes when
        reconciling a lost answer.
        """
        config = node.config if isinstance(node.config, Mapping) else {}
        declared = config.get("tool_name") if node.type == "tool" else config.get("model")
        self._repo.mark_step_dispatch_intent(
            self._ctx(actor),
            execution_id=claim.execution_id,
            node_id=node.id,
            attempt=attempt,
            fence=claim.fence,
            # The key is the one the engine hands the adapter, so both agree.
            tool_call_key=f"{claim.execution_id}:{node.id}",
            tool_id=str(declared) if declared else None,
        )

    def _persist_step(
        self, actor: RuntimeActor, claim: ExecutionClaim, step: StepOutcome, attempt: int
    ) -> None:
        """Persist one settled step immediately, under the claim's fence.

        The input travels with the settlement: the activation the node was
        dispatched with is written to the execution's append-only payload ledger
        (kind ``step_input``) and referenced from the step row, so one read of the
        detail shows what every node was actually given.
        """
        ctx = self._ctx(actor)
        output_sha, _ = _hash_json(step.output) if step.output is not None else (None, 0)
        input_sha: str | None = None
        input_size = 0
        if step.input is not None:
            input_sha, input_size = _hash_json(step.input)
        with runtime_transaction(self._db, ctx) as conn:
            # An input over the platform's per-payload ceiling is pinned by its
            # digest alone: the ledger keeps facts, not unbounded copies of them.
            input_payload_id = (
                self._repo.insert_payload(
                    ctx,
                    tenant_id=claim.tenant_id,
                    execution_id=claim.execution_id,
                    kind="step_input",
                    node_id=step.node_id,
                    content=step.input,
                    sha256=input_sha,
                    size_bytes=input_size,
                    conn=conn,
                )
                if input_sha is not None and input_size <= DEFAULT_MAX_OUTPUT_BYTES
                else None
            )
            if self._repo.settle_attempt_step(
                ctx,
                execution_id=claim.execution_id,
                node_id=step.node_id,
                attempt=attempt,
                fence=claim.fence,
                status=step.status,
                save_as=step.save_as,
                input_sha256=input_sha,
                input_payload_id=input_payload_id,
                output_sha256=output_sha,
                output=step.output,
                token_usage=step.token_usage,
                error_code=step.error_code,
                error_message=step.error_message,
                duration_ms=step.duration_ms,
                skip_reason=step.skip_reason,
                started_at=step.started_at,
                conn=conn,
            ):
                return
            if not self._repo.verify_fence(
                ctx,
                tenant_id=claim.tenant_id,
                lease_name=execution_lease_name(claim.execution_id),
                holder=claim.worker_id,
                fence=claim.fence,
            ):
                raise OctopError(
                    ErrorCode.WORKBUDDY_FENCE_STALE,
                    "the execution lease moved to another runner",
                )
            # The lease is ours, so this step simply had no pending row (an outcome
            # the engine decided without announcing a start): record it outright.
            self._repo.insert_step_run(
                ctx,
                tenant_id=claim.tenant_id,
                execution_id=claim.execution_id,
                node_id=step.node_id,
                node_type=step.node_type or "transform",
                status=step.status,
                fence=claim.fence,
                attempt=attempt,
                save_as=step.save_as,
                input_sha256=input_sha,
                input_payload_id=input_payload_id,
                output_sha256=output_sha,
                output=step.output,
                token_usage=step.token_usage,
                error_code=step.error_code,
                error_message=step.error_message,
                duration_ms=step.duration_ms,
                skip_reason=step.skip_reason,
                started_at=step.started_at,
                conn=conn,
            )

    def _close_claim(self, claim: ExecutionClaim, lease_name: str) -> None:
        """Give back a claim whose execution no longer needs running."""
        actor = RuntimeActor(
            tenant_id=claim.tenant_id,
            user_id=None,
            role="member",
            tenant_status="active",
            kind="system",
            platform=True,
        )
        ctx = self._ctx(actor)
        with runtime_transaction(self._db, ctx) as conn:
            if not self._repo.verify_fence(
                ctx,
                tenant_id=claim.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=claim.fence,
                conn=conn,
            ):
                return
            self._settle_execution_quota(actor, claim.execution_id, conn=conn, consume=True)
            self._repo.release_lease(
                ctx,
                tenant_id=claim.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=claim.fence,
                conn=conn,
            )

    def _fail_claimed_execution(
        self,
        actor: RuntimeActor,
        execution: ExecutionRow,
        claim: ExecutionClaim,
        lease_name: str,
        error: Exception,
    ) -> None:
        """Fail an execution whose locked definition cannot be interpreted."""
        ctx = self._ctx(actor)
        with runtime_transaction(self._db, ctx) as conn:
            if not self._repo.verify_fence(
                ctx,
                tenant_id=claim.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=claim.fence,
                conn=conn,
            ):
                return
            self._repo.update_execution_status(
                ctx,
                execution.id,
                status="failed",
                expected_status=("running",),
                expected_fence=claim.fence,
                error_code=ErrorCode.WORKBUDDY_VALIDATION_FAILED.value,
                error_message=str(error)[:500],
                mark_finished=True,
                conn=conn,
            )
            self._settle_execution_quota(actor, execution.id, conn=conn, consume=True)
            self._audit(
                actor,
                action="execution.finish",
                resource_type="execution",
                resource_id=execution.id,
                outcome="failed",
                details={"status": "failed", "error_code": "WORKBUDDY_VALIDATION_FAILED"},
                conn=conn,
            )
            self._repo.release_lease(
                ctx,
                tenant_id=claim.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=claim.fence,
                conn=conn,
            )

    def _finalize(
        self,
        actor: RuntimeActor,
        execution: ExecutionRow,
        run: GraphRun,
        graph: WorkflowGraph,
        *,
        claim: ExecutionClaim,
        lease_name: str,
    ) -> None:
        """Commit the attempt inside one fenced transaction, or commit nothing."""
        ctx = self._ctx(actor)
        fence = claim.fence

        with runtime_transaction(self._db, ctx) as conn:
            if not self._repo.verify_fence(
                ctx,
                tenant_id=actor.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=fence,
                conn=conn,
            ):
                raise OctopError(
                    ErrorCode.WORKBUDDY_FENCE_STALE,
                    "execution lease was taken over by another runner",
                )
            self._persist_run(
                actor,
                execution,
                run,
                graph,
                conn=conn,
            )
            if run.status == "waiting_reconciliation":
                # The write may have happened; nothing downstream may run, and no
                # retry is offered, until an operator reconciles the evidence.
                self._repo.update_execution_status(
                    ctx,
                    execution.id,
                    status="waiting_reconciliation",
                    expected_status=("running",),
                    expected_fence=fence,
                    outputs=run.outputs,
                    conn=conn,
                )
                self._repo.enqueue_outbox(
                    ctx,
                    tenant_id=actor.tenant_id,
                    topic="workbuddy.execution.finished",
                    dedupe_key=f"{execution.id}:waiting_reconciliation",
                    payload={
                        "execution_id": execution.id,
                        "status": "waiting_reconciliation",
                        "node_id": run.reconciliation_node_id,
                    },
                    conn=conn,
                )
                self._audit(
                    actor,
                    action="execution.wait_reconciliation",
                    resource_type="execution",
                    resource_id=execution.id,
                    details={"node_id": run.reconciliation_node_id},
                    conn=conn,
                )
                # A parked execution is waiting for an operator, not running, so
                # it holds no running slot; the reconciliation re-queues it.
                self._release_concurrency_slot(actor, execution.id, conn=conn)
                self._repo.release_lease(
                    ctx,
                    tenant_id=actor.tenant_id,
                    lease_name=lease_name,
                    holder=claim.worker_id,
                    fence=fence,
                    conn=conn,
                )
                return
            if run.status == "waiting_approval" and run.waiting_approval_node_id is not None:
                self._open_approval(
                    actor,
                    execution,
                    run.waiting_approval_node_id,
                    run.approval_candidates.get(run.waiting_approval_node_id, ()),
                    conn=conn,
                )
                self._repo.update_execution_status(
                    ctx,
                    execution.id,
                    status="waiting_approval",
                    expected_status=("running",),
                    expected_fence=fence,
                    conn=conn,
                )
                self._release_concurrency_slot(actor, execution.id, conn=conn)
                self._repo.release_lease(
                    ctx,
                    tenant_id=actor.tenant_id,
                    lease_name=lease_name,
                    holder=claim.worker_id,
                    fence=fence,
                    conn=conn,
                )
                return
            if run.status == "waiting_input" and run.waiting_input_node_id is not None:
                self._open_input_request(
                    actor,
                    execution,
                    run.waiting_input_node_id,
                    run.input_assignees.get(run.waiting_input_node_id, ()),
                    conn=conn,
                )
                self._repo.update_execution_status(
                    ctx,
                    execution.id,
                    status="waiting_input",
                    expected_status=("running",),
                    expected_fence=fence,
                    conn=conn,
                )
                self._release_concurrency_slot(actor, execution.id, conn=conn)
                self._repo.release_lease(
                    ctx,
                    tenant_id=actor.tenant_id,
                    lease_name=lease_name,
                    holder=claim.worker_id,
                    fence=fence,
                    conn=conn,
                )
                return
            self._repo.update_execution_status(
                ctx,
                execution.id,
                status=run.status,
                expected_status=("running",),
                expected_fence=fence,
                error_code=run.error_code,
                error_message=run.error_message,
                outputs=run.outputs,
                # Active time is what the steps measured, never the wait for a
                # human or an operator.
                active_duration_ms=sum(step.duration_ms or 0 for step in run.steps),
                token_usage=run.tokens,
                mark_finished=True,
                conn=conn,
            )
            self._settle_execution_quota(actor, execution.id, conn=conn, consume=True)
            self._repo.enqueue_outbox(
                ctx,
                tenant_id=actor.tenant_id,
                topic="workbuddy.execution.finished",
                dedupe_key=f"{execution.id}:{run.status}",
                payload={"execution_id": execution.id, "status": run.status},
                conn=conn,
            )
            if execution.created_by_user_id is not None:
                self._repo.insert_notification(
                    ctx,
                    tenant_id=actor.tenant_id,
                    user_id=execution.created_by_user_id,
                    kind="execution.finished",
                    title=f"Execution {run.status}",
                    body=run.error_message,
                    resource_type="execution",
                    resource_id=execution.id,
                    conn=conn,
                )
            self._audit(
                actor,
                action="execution.finish",
                resource_type="execution",
                resource_id=execution.id,
                outcome="allowed" if run.status == "success" else "failed",
                details={"status": run.status, "error_code": run.error_code},
                conn=conn,
            )
            self._repo.release_lease(
                ctx,
                tenant_id=actor.tenant_id,
                lease_name=lease_name,
                holder=claim.worker_id,
                fence=fence,
                conn=conn,
            )

    def _open_approval(
        self,
        actor: RuntimeActor,
        execution: ExecutionRow,
        node_id: str,
        candidates: Sequence[tuple[int, str | None]],
        *,
        conn: Any,
    ) -> str:
        ctx = self._ctx(actor)
        graph = self._graph_from_snapshot(execution)
        node = graph.node(node_id)
        if node is None:
            raise _invalid("approval node is not part of the locked workflow version")
        approvers, timeout_hours = approval_requirements(node)
        required = len(candidates)
        params = {
            "node_id": node_id,
            "node_name": node.name,
            "approval_message": str(node.config.get("approval_message") or "")[:500],
            "approver_membership_ids": list(approvers),
            "timeout_hours": timeout_hours,
            "workflow_id": execution.workflow_id,
            "workflow_version_id": execution.workflow_version_id,
            "inputs": execution.inputs,
        }
        params_sha, _ = _hash_json(params)
        approval_id = self._repo.insert_approval_request(
            ctx,
            tenant_id=actor.tenant_id,
            execution_id=execution.id,
            node_id=node_id,
            required_approvals=required,
            params=params,
            params_sha256=params_sha,
            locked_workflow_version_id=execution.workflow_version_id,
            locked_workflow_version_hash=execution.workflow_version_hash,
            conn=conn,
        )
        self._repo.insert_approval_candidates(
            ctx,
            tenant_id=actor.tenant_id,
            approval_request_id=approval_id,
            candidates=candidates,
            conn=conn,
        )
        for user_id, _department in candidates:
            self._repo.insert_notification(
                ctx,
                tenant_id=actor.tenant_id,
                user_id=user_id,
                kind="approval.requested",
                title=f"Approval required: {node.name}",
                resource_type="approval_request",
                resource_id=approval_id,
                conn=conn,
            )
        self._repo.enqueue_outbox(
            ctx,
            tenant_id=actor.tenant_id,
            topic="workbuddy.approval.created",
            dedupe_key=f"{approval_id}:created",
            payload={"approval_request_id": approval_id, "execution_id": execution.id},
            conn=conn,
        )
        self._audit(
            actor,
            action="approval.request",
            resource_type="approval_request",
            resource_id=approval_id,
            details={"execution_id": execution.id, "node_id": node_id},
            conn=conn,
        )
        return approval_id

    def _open_input_request(
        self,
        actor: RuntimeActor,
        execution: ExecutionRow,
        node_id: str,
        assignees: Sequence[tuple[int, str | None]],
        *,
        conn: Any,
    ) -> str:
        """Record the form a parked execution is waiting on, and tell its assignees.

        The locked version id and hash are copied onto the request for the same
        reason an approval copies them: the definition that asked the question is
        the one the answer belongs to, so a later publish cannot reinterpret it.
        """
        ctx = self._ctx(actor)
        graph = self._graph_from_snapshot(execution)
        node = graph.node(node_id)
        if node is None:
            raise _invalid("ask node is not part of the locked workflow version")
        _assignees, timeout_hours = ask_requirements(node)
        fields = [dict(field) for field in node.config.get("fields") or ()]
        # The question is rendered the moment it is asked: an assignee has to read
        # a question about *this* run, not the template that produced it.  The
        # recorded outputs are the ones this attempt has already settled, which is
        # exactly the context the template was validated against.
        prompt = str(
            render_template(
                node.config.get("prompt"),
                inputs=execution.inputs,
                node_results=self._repo.recorded_outputs(ctx, execution.id),
            )
            or ""
        )[:1000]
        form = {
            "node_id": node_id,
            "node_name": node.name,
            "prompt": prompt,
            "fields": fields,
            "assignee_membership_ids": [user_id for user_id, _dept in assignees],
            "timeout_hours": timeout_hours,
            "workflow_id": execution.workflow_id,
            "workflow_version_id": execution.workflow_version_id,
            "inputs": execution.inputs,
        }
        form_sha, _ = _hash_json(form)
        request_id = self._repo.insert_input_request(
            ctx,
            tenant_id=actor.tenant_id,
            execution_id=execution.id,
            node_id=node_id,
            prompt=prompt,
            form=form,
            form_sha256=form_sha,
            expires_at=datetime.now(UTC) + timedelta(hours=timeout_hours),
            locked_workflow_version_id=execution.workflow_version_id,
            locked_workflow_version_hash=execution.workflow_version_hash,
            conn=conn,
        )
        self._repo.insert_input_assignees(
            ctx,
            tenant_id=actor.tenant_id,
            input_request_id=request_id,
            assignees=assignees,
            conn=conn,
        )
        for user_id, _department in assignees:
            self._repo.insert_notification(
                ctx,
                tenant_id=actor.tenant_id,
                user_id=user_id,
                kind="input.requested",
                title=f"Input required: {node.name}",
                resource_type="input_request",
                resource_id=request_id,
                conn=conn,
            )
        self._repo.enqueue_outbox(
            ctx,
            tenant_id=actor.tenant_id,
            topic="workbuddy.input_request.created",
            dedupe_key=f"{request_id}:created",
            payload={"input_request_id": request_id, "execution_id": execution.id},
            conn=conn,
        )
        self._audit(
            actor,
            action="input.request",
            resource_type="input_request",
            resource_id=request_id,
            details={"execution_id": execution.id, "node_id": node_id},
            conn=conn,
        )
        return request_id

    def get_execution(self, actor: RuntimeActor, execution_id: str) -> ExecutionView:
        self._require_postgres()
        return _execution_view(self._load_execution(actor, execution_id))

    def list_executions(
        self,
        actor: RuntimeActor,
        *,
        scope: str = "self",
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ExecutionView]:
        self._require_postgres()
        if status is not None and status not in EXECUTION_STATUSES:
            raise _invalid("unknown execution status filter")
        tenant_scope = scope == "tenant"
        if tenant_scope and not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "tenant scope requires tenant admin")
        ctx = self._ctx(actor)
        rows = self._repo.list_executions(
            ctx,
            created_by_user_id=None if tenant_scope else actor.user_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
        )
        if tenant_scope:
            self._audit(
                actor,
                action="execution.list_tenant",
                resource_type="execution",
                resource_id=None,
                details={"count": len(rows)},
            )
        return [_execution_view(row) for row in rows]

    def execution_facts(
        self, actor: RuntimeActor, execution_id: str
    ) -> tuple[ExecutionView, list[StepRunView], list[Mapping[str, Any]]]:
        self._require_postgres()
        execution = self._load_execution(actor, execution_id)
        ctx = self._ctx(actor)
        # One read for the whole execution: each step points at the payload that
        # holds the input it was dispatched with.
        step_inputs = self._repo.list_step_inputs(ctx, execution_id)
        steps = [
            StepRunView(
                node_id=step.node_id,
                node_type=step.node_type,
                attempt=step.attempt,
                status=step.status,
                save_as=step.save_as,
                error_code=step.error_code,
                skip_reason=step.skip_reason,
                started_at=_iso(step.started_at),
                finished_at=_iso(step.finished_at),
                duration_ms=step.duration_ms,
                tool_id=step.tool_id,
                tool_call_key=step.tool_call_key,
                dispatch_intent_at=_iso(step.dispatch_intent_at),
                input=(
                    step_inputs.get(step.input_payload_id)
                    if step.input_payload_id is not None
                    else None
                ),
                input_sha256=step.input_sha256,
                output=step.output,
                token_usage=step.token_usage,
            )
            for step in self._repo.list_step_runs(ctx, execution_id)
        ]
        edges: list[Mapping[str, Any]] = [
            {
                "from": edge.edge_from,
                "to": edge.edge_to,
                "branch": edge.branch,
                "taken": edge.taken,
            }
            for edge in self._repo.list_edge_runs(ctx, execution_id)
        ]
        return _execution_view(execution), steps, edges

    def cancel_execution(self, actor: RuntimeActor, execution_id: str) -> ExecutionView:
        self._require_postgres()
        execution = self._load_execution(actor, execution_id)
        ctx = self._ctx(actor)
        if execution.status == "waiting_reconciliation":
            # Cancelling cannot undo a write that may already have happened, so
            # the request is recorded and the execution converges to canceled
            # once the evidence is reconciled.
            if not self._repo.request_cancel_deferred(ctx, execution_id):
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "cancel was already requested for this execution",
                )
            self._audit(
                actor,
                action="execution.cancel_requested",
                resource_type="execution",
                resource_id=execution_id,
            )
            updated = self._repo.get_execution(ctx, execution_id)
            if updated is None:  # pragma: no cover - defensive
                raise _not_found()
            return _execution_view(updated)
        with runtime_transaction(self._db, ctx) as conn:
            if execution.status == "running":
                if not self._repo.request_cancel_running(ctx, execution_id, conn=conn):
                    raise OctopError(
                        ErrorCode.STATE_CONFLICT,
                        "execution is already finished",
                    )
            elif not self._repo.request_cancel(ctx, execution_id, conn=conn):
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "execution is already finished",
                )
            else:
                self._repo.invalidate_pending_approvals(ctx, execution_id, conn=conn)
                self._settle_execution_quota(actor, execution_id, conn=conn, consume=True)
                self._repo.enqueue_outbox(
                    ctx,
                    tenant_id=actor.tenant_id,
                    topic="workbuddy.execution.finished",
                    dedupe_key=f"{execution_id}:cancelled",
                    payload={"execution_id": execution_id, "status": "canceled"},
                    conn=conn,
                )
            self._audit(
                actor,
                action="execution.cancel",
                resource_type="execution",
                resource_id=execution_id,
                conn=conn,
            )
        updated = self._repo.get_execution(ctx, execution_id)
        if updated is None:
            raise _not_found()
        return _execution_view(updated)

    def issue_approval_challenge(self, actor: RuntimeActor, approval_request_id: str) -> str:
        """Issue a two-minute one-time resume token; only its hash is persisted."""
        self._require_postgres()
        approval = self._load_approval(actor, approval_request_id)
        if approval.status != "pending":
            raise OctopError(
                ErrorCode.APPROVAL_ALREADY_DECIDED,
                "approval request is no longer pending",
            )
        token = secrets.token_urlsafe(32)
        register_secret(token)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(seconds=APPROVAL_TOKEN_TTL_SECONDS)
        ctx = self._ctx(actor)
        if not self._repo.issue_approval_challenge(
            ctx, approval_request_id, token_hash=token_hash, expires_at=expires_at
        ):
            # The request moved between the read and the CAS.
            raise OctopError(
                ErrorCode.STATE_CONFLICT,
                "approval request changed while the challenge was issued",
            )
        self._audit(
            actor,
            action="approval.challenge",
            resource_type="approval_request",
            resource_id=approval_request_id,
        )
        return token

    def resume_execution(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        approval_request_id: str,
        decision: str,
        token: str,
    ) -> ExecutionView:
        """Apply one approval decision and re-queue the execution for admission.

        A parked execution holds no running slot, so the decision only moves it
        back to ``queued``: the worker admits it again when the tenant has a slot
        free, exactly like the first attempt.
        """
        self._require_postgres()
        if decision not in APPROVAL_DECISIONS:
            raise _invalid("decision must be 'approved' or 'rejected'")
        ctx = self._ctx(actor)
        with runtime_transaction(self._db, ctx) as conn:
            execution = self._load_execution(actor, execution_id, conn=conn)
            if execution.status == "waiting_reconciliation":
                raise OctopError(
                    ErrorCode.RECONCILIATION_REQUIRED,
                    "an unknown external write must be reconciled before resuming",
                )
            if execution.status != "waiting_approval":
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "execution is not waiting for an approval decision",
                )
            approval = self._load_approval(actor, approval_request_id, conn=conn)
            if approval.execution_id != execution_id:
                raise _not_found("approval request does not belong to this execution")
            if approval.locked_workflow_version_id != execution.workflow_version_id:
                raise OctopError(
                    ErrorCode.APPROVAL_VERSION_MISMATCH,
                    "approval request was bound to a different workflow version",
                )
            if approval.status != "pending":
                raise OctopError(
                    ErrorCode.APPROVAL_ALREADY_DECIDED,
                    "approval request has already been decided",
                )
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not self._repo.consume_approval_token(
                ctx,
                approval_request_id,
                token_hash=token_hash,
                conn=conn,
            ):
                raise OctopError(
                    ErrorCode.APPROVAL_TOKEN_INVALID,
                    "approval token is invalid, expired, or already used",
                )
            if not self._repo.decide_candidate(
                ctx,
                approval_request_id,
                user_id=actor.acting_user_id,
                decision=decision,
                conn=conn,
            ):
                raise OctopError(
                    ErrorCode.FORBIDDEN_NOT_APPROVER,
                    "this user is not a pending candidate for the approval",
                )
            decided = self._repo.count_decided_approvals(ctx, approval_request_id, conn=conn)
            if decision == "rejected":
                self._repo.settle_approval_request(
                    ctx,
                    approval_request_id,
                    status="rejected",
                    decision="rejected",
                    decided_by_user_id=actor.acting_user_id,
                    decided_approvals=decided,
                    conn=conn,
                )
            elif decided >= approval.required_approvals:
                self._repo.settle_approval_request(
                    ctx,
                    approval_request_id,
                    status="approved",
                    decision="approved",
                    decided_by_user_id=actor.acting_user_id,
                    decided_approvals=decided,
                    conn=conn,
                )
            else:
                self._audit(
                    actor,
                    action="approval.decision",
                    resource_type="approval_request",
                    resource_id=approval_request_id,
                    details={"decision": decision, "decided": decided},
                    conn=conn,
                )
                return _execution_view(execution)

            self._repo.requeue_execution(
                ctx,
                execution_id,
                expected_status=("waiting_approval",),
                conn=conn,
            )
            self._audit(
                actor,
                action="approval.decision",
                resource_type="approval_request",
                resource_id=approval_request_id,
                details={"decision": decision, "decided": decided},
                conn=conn,
            )
            requeued = self._repo.get_execution(ctx, execution_id, conn=conn)
        if requeued is None:  # pragma: no cover - defensive
            raise _not_found()
        return _execution_view(requeued)

    def submit_input(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        input_request_id: str,
        values: Mapping[str, Any],
    ) -> ExecutionView:
        """Record one answer and re-queue the execution for admission.

        An answer settles like an approval decision: the parked execution holds no
        running slot, so the write only moves it back to ``queued`` and the worker
        admits it again when the tenant has one free.  What differs is who may
        write it (an assignee, with no token to hold) and what is written (values
        the form the run asked with is checked against).
        """
        self._require_postgres()
        ctx = self._ctx(actor)
        with runtime_transaction(self._db, ctx) as conn:
            execution = self._load_execution(actor, execution_id, conn=conn)
            if execution.status != "waiting_input":
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "execution is not waiting for an answer",
                )
            request = self._load_input_request(actor, input_request_id, conn=conn)
            if request.execution_id != execution_id:
                raise _not_found()
            if request.locked_workflow_version_id != execution.workflow_version_id:
                # The answer belongs to the question the locked version asked; a
                # different version means the row no longer describes this run.
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "input request was bound to a different workflow version",
                )
            if request.status != "open":
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "input request has already been answered",
                )
            cleaned = validate_answers(request.form, values)
            values_sha, _ = _hash_json(cleaned)
            if not self._repo.submit_input_request(
                ctx,
                input_request_id=input_request_id,
                values=cleaned,
                values_sha256=values_sha,
                submitted_by_user_id=actor.acting_user_id,
                conn=conn,
            ):
                # Another answer won the compare-and-set after the read above:
                # exactly one answer may ever be recorded for one question.
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "input request has already been answered",
                )
            self._repo.mark_input_assignee_answered(
                ctx,
                input_request_id=input_request_id,
                user_id=actor.acting_user_id,
                conn=conn,
            )
            # The answer is the improvement loop's other kind of raw material: a
            # fact a person supplied, recorded with the run and version it belongs
            # to so the analyser can tell it apart from a correction (A-12).
            self._feedback.insert_execution_feedback(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution_id,
                workflow_id=execution.workflow_id,
                workflow_version_id=execution.workflow_version_id,
                workflow_version_hash=execution.workflow_version_hash,
                kind="supplied_fact",
                source="ask",
                source_id=input_request_id,
                node_id=request.node_id,
                after=cleaned,
                created_by_user_id=actor.acting_user_id,
                conn=conn,
            )
            self._repo.requeue_execution(
                ctx,
                execution_id,
                expected_status=("waiting_input",),
                conn=conn,
            )
            self._audit(
                actor,
                action="input.submit",
                resource_type="input_request",
                resource_id=input_request_id,
                details={"execution_id": execution_id, "node_id": request.node_id},
                conn=conn,
            )
            requeued = self._repo.get_execution(ctx, execution_id, conn=conn)
        if requeued is None:  # pragma: no cover - defensive
            raise _not_found()
        return _execution_view(requeued)

    def list_execution_input_requests(
        self, actor: RuntimeActor, execution_id: str
    ) -> list[dict[str, Any]]:
        """Every question one execution asked, newest first, with its assignees.

        Reading the execution first is what keeps the 404 honest: an actor who
        cannot see the run cannot enumerate the questions it asked either.
        """
        self._require_postgres()
        ctx = self._ctx(actor)
        self._load_execution(actor, execution_id)
        rows = self._repo.list_input_requests(ctx, execution_id=execution_id, limit=200)
        return [
            input_request_payload(row, self._repo.list_input_assignees(ctx, row.id)) for row in rows
        ]

    def list_input_requests(
        self,
        actor: RuntimeActor,
        *,
        scope: str = "self",
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The questions the caller may read, newest first.

        ``scope="self"`` is the answering queue: only what is assigned to this
        member.  ``scope="tenant"`` is the operator view, needs an admin, and is
        audited — a member must never read another member's questions.
        """
        self._require_postgres()
        if scope not in {"self", "tenant"}:
            raise _invalid("scope must be 'self' or 'tenant'")
        tenant_scope = scope == "tenant"
        if tenant_scope and not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "tenant scope requires tenant admin")
        ctx = self._ctx(actor)
        rows = self._repo.list_input_requests(
            ctx,
            assignee_user_id=None if tenant_scope else actor.user_id,
            status=status,
            limit=limit,
        )
        if tenant_scope:
            self._audit(
                actor,
                action="input.list_tenant",
                resource_type="input_request",
                resource_id=None,
                details={"count": len(rows)},
            )
        return [input_request_payload(row) for row in rows]

    def expire_overdue_input_requests(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Expire questions past their deadline and hand their runs back to the worker.

        The escalation is the point of the sweep: a question nobody answered in time
        is recorded as expired, the people who could still act are told, and the run
        is re-queued so its next engine attempt fails that node
        (``INPUT_REQUEST_EXPIRED``) instead of parking on the same question again.
        A deadline that passes silently would leave the run waiting forever, which
        is exactly what a timeout rule exists to prevent.
        """
        self._require_postgres()
        expired = self._repo.claim_expired_input_requests(limit=limit)
        escalated: list[dict[str, Any]] = []
        for request in expired:
            ctx = WorkBuddyDbContext.for_tenant(request.tenant_id)
            notified = self._escalate_expired_question(ctx, request)
            if not self._repo.requeue_execution(
                ctx, request.execution_id, expected_status=("waiting_input",)
            ):
                # The execution is no longer parked on this question (an answer or
                # a cancel won the race), so there is nothing to hand back to the
                # worker: the escalation above still stands, and the run settles
                # through whichever path already took it.
                logger.info(
                    "input request %s expired but execution %s is no longer waiting for it",
                    request.id,
                    request.execution_id,
                )
            self._repo.append_audit(
                ctx,
                tenant_id=request.tenant_id,
                actor_user_id=None,
                actor_kind="system",
                action="input.expire",
                resource_type="input_request",
                resource_id=request.id,
                outcome="allowed",
                details={
                    "execution_id": request.execution_id,
                    "node_id": request.node_id,
                    "notified": notified,
                },
            )
            escalated.append(input_request_payload(request))
        return escalated

    def _escalate_expired_question(self, ctx: WorkBuddyDbContext, request: InputRequestRow) -> int:
        """Tell the assignees and the tenant's admins that a question timed out."""
        from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

        recipients = {
            assignee.user_id for assignee in self._repo.list_input_assignees(ctx, request.id)
        }
        for member in WorkBuddyIdentityRepo(self._db).list_members(request.tenant_id):
            user_id = member.get("user_id")
            if user_id is None:
                continue
            if str(member.get("role") or "") in TENANT_ADMIN_ROLES:
                recipients.add(int(user_id))
        for user_id in sorted(recipients):
            self._repo.insert_notification(
                ctx,
                tenant_id=request.tenant_id,
                user_id=user_id,
                kind="input.expired",
                title=f"Input overdue: {request.node_id}",
                resource_type="input_request",
                resource_id=request.id,
            )
        self._repo.enqueue_outbox(
            ctx,
            tenant_id=request.tenant_id,
            topic="workbuddy.input_request.expired",
            dedupe_key=f"{request.id}:expired",
            payload={"input_request_id": request.id, "execution_id": request.execution_id},
        )
        return len(recipients)

    def _resolve_reviewers(
        self, actor: RuntimeActor, membership_ids: Sequence[str]
    ) -> list[tuple[int, str | None]]:
        """Identity ids to the current members who may review, or a refusal."""
        if self._member_resolver is None:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "reviewer resolution is not configured for this deployment",
            )
        resolved = {
            int(user_id): department_id
            for user_id, department_id in self._member_resolver(
                actor.tenant_id, list(membership_ids)
            )
        }
        return sorted(resolved.items())

    def _load_output_review(
        self, actor: RuntimeActor, execution_id: str, *, conn: Any = None
    ) -> OutputReviewRow:
        """One review the actor may read, or a 404.

        Readable by the people asked to review it, by whoever asked for it, and by
        admins; everyone else learns nothing about whether it exists.
        """
        ctx = self._ctx(actor)
        row = self._repo.get_output_review_for_execution(ctx, execution_id, conn=conn)
        if row is None:
            raise _not_found()
        if actor.is_admin or row.requested_by_user_id == actor.user_id:
            return row
        reviewers = self._repo.list_output_review_reviewers(ctx, row.id, conn=conn)
        if all(reviewer.user_id != actor.user_id for reviewer in reviewers):
            raise _not_found()
        return row

    def request_output_review(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        reviewer_membership_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Ask people to look at what a settled run produced.

        The run stays exactly as it settled and its outputs are copied, not moved:
        a review adds a second, append-only account of what a person said about the
        result.  That is what keeps ``correct`` and ``rerun`` honest — an audit can
        always tell the workflow's own output from a correction recorded later.
        """
        self._require_postgres()
        named = [str(member_id) for member_id in reviewer_membership_ids if str(member_id)]
        if not named:
            raise _invalid("a review needs at least one reviewer")
        ctx = self._ctx(actor)
        with runtime_transaction(self._db, ctx) as conn:
            execution = self._load_execution(actor, execution_id, conn=conn)
            if execution.status not in TERMINAL_EXECUTION_STATUSES:
                raise OctopError(
                    ErrorCode.STATE_CONFLICT,
                    "only a settled execution can be reviewed",
                )
            if self._repo.get_output_review_for_execution(ctx, execution.id, conn=conn) is not None:
                raise OctopError(ErrorCode.STATE_CONFLICT, "this execution already has a review")
            reviewers = self._resolve_reviewers(actor, named)
            if not reviewers:
                raise OctopError(
                    ErrorCode.WORKBUDDY_VALIDATION_FAILED,
                    "no named reviewer is a current member of this tenant",
                )
            produced = dict(execution.outputs or {})
            produced_sha, _ = _hash_json(produced)
            review_id = self._repo.insert_output_review(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution.id,
                produced=produced,
                produced_sha256=produced_sha,
                requested_by_user_id=actor.acting_user_id,
                locked_workflow_version_id=execution.workflow_version_id,
                locked_workflow_version_hash=execution.workflow_version_hash,
                conn=conn,
            )
            self._repo.insert_output_review_reviewers(
                ctx,
                tenant_id=actor.tenant_id,
                review_id=review_id,
                reviewers=reviewers,
                conn=conn,
            )
            for user_id, _department in reviewers:
                self._repo.insert_notification(
                    ctx,
                    tenant_id=actor.tenant_id,
                    user_id=user_id,
                    kind="output_review.requested",
                    title=f"Review required: {execution.workflow_id}",
                    resource_type="output_review",
                    resource_id=review_id,
                    conn=conn,
                )
            self._repo.enqueue_outbox(
                ctx,
                tenant_id=actor.tenant_id,
                topic="workbuddy.output_review.created",
                dedupe_key=f"{review_id}:created",
                payload={"review_id": review_id, "execution_id": execution.id},
                conn=conn,
            )
            self._audit(
                actor,
                action="output_review.request",
                resource_type="output_review",
                resource_id=review_id,
                details={"execution_id": execution.id, "reviewers": len(reviewers)},
                conn=conn,
            )
            row = self._repo.get_output_review(ctx, review_id, conn=conn)
            reviewer_rows = self._repo.list_output_review_reviewers(ctx, review_id, conn=conn)
        if row is None:  # pragma: no cover - defensive
            raise _not_found()
        return output_review_payload(row, reviewer_rows)

    def get_output_review(self, actor: RuntimeActor, execution_id: str) -> dict[str, Any]:
        """The review of one execution, with its reviewers."""
        self._require_postgres()
        ctx = self._ctx(actor)
        row = self._load_output_review(actor, execution_id)
        return output_review_payload(row, self._repo.list_output_review_reviewers(ctx, row.id))

    def decide_output_review(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        decision: str,
        corrected: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record what a reviewer decided about a run's output.

        ``accept`` confirms the output as produced.  ``correct`` stores the values
        a person fixed — only keys the run actually produced, because a correction
        is not a place to invent results.  ``rerun`` starts a new execution and
        links it here, so "run it again" is a fact about two runs rather than a
        rewrite of one.

        ``rerun`` refuses when the workflow has been republished since the reviewed
        run: the correction belongs to the version that produced the output, and
        running the current one would answer a different question.
        """
        self._require_postgres()
        if decision not in {"accept", "correct", "rerun"}:
            raise _invalid("decision must be 'accept', 'correct' or 'rerun'")
        ctx = self._ctx(actor)
        rerun_inputs: dict[str, Any] | None = None
        with runtime_transaction(self._db, ctx) as conn:
            execution = self._load_execution(actor, execution_id, conn=conn)
            review = self._load_output_review(actor, execution_id, conn=conn)
            if review.status != "open":
                raise OctopError(ErrorCode.STATE_CONFLICT, "this review has already been decided")
            if decision == "rerun":
                active = self._versions.load_active(ctx, execution.workflow_id)
                if active.version_id != review.locked_workflow_version_id:
                    raise OctopError(
                        ErrorCode.STATE_CONFLICT,
                        "the workflow was republished since this run; re-run it explicitly",
                    )
                rerun_inputs = dict(inputs) if inputs is not None else dict(execution.inputs or {})
            if decision == "correct":
                values = validate_correction(review.produced, corrected)
                values_sha, _ = _hash_json(values)
                settled_status: str = "corrected"
            else:
                values, values_sha = None, None
                settled_status = "accepted" if decision == "accept" else "rerun"
            if not self._repo.decide_output_review(
                ctx,
                review_id=review.id,
                status=settled_status,
                corrected=values,
                corrected_sha256=values_sha,
                decided_by_user_id=actor.acting_user_id,
                conn=conn,
            ):
                raise OctopError(ErrorCode.STATE_CONFLICT, "this review has already been decided")
            self._repo.mark_output_review_reviewer(
                ctx,
                review_id=review.id,
                user_id=actor.acting_user_id,
                decision=settled_status,
                conn=conn,
            )
            # One structured row per corrected key, bound to the version that
            # produced it: that binding is what lets the improvement loop say
            # "this workflow, at this version, was wrong here" (A-12).
            for corrected_key, corrected_value in sorted((values or {}).items()):
                self._feedback.insert_execution_feedback(
                    ctx,
                    tenant_id=actor.tenant_id,
                    execution_id=execution_id,
                    workflow_id=execution.workflow_id,
                    workflow_version_id=execution.workflow_version_id,
                    workflow_version_hash=execution.workflow_version_hash,
                    kind="correction",
                    source="output_review",
                    source_id=review.id,
                    output_key=corrected_key,
                    before=dict(review.produced).get(corrected_key),
                    after=corrected_value,
                    created_by_user_id=actor.acting_user_id,
                    conn=conn,
                )
            if review.requested_by_user_id is not None:
                self._repo.insert_notification(
                    ctx,
                    tenant_id=actor.tenant_id,
                    user_id=review.requested_by_user_id,
                    kind="output_review.decided",
                    title=f"Review {settled_status}: {execution.workflow_id}",
                    resource_type="output_review",
                    resource_id=review.id,
                    conn=conn,
                )
            self._repo.enqueue_outbox(
                ctx,
                tenant_id=actor.tenant_id,
                topic="workbuddy.output_review.decided",
                dedupe_key=f"{review.id}:decided",
                payload={
                    "review_id": review.id,
                    "execution_id": execution.id,
                    "status": settled_status,
                },
                conn=conn,
            )
            self._audit(
                actor,
                action="output_review.decide",
                resource_type="output_review",
                resource_id=review.id,
                details={
                    "execution_id": execution.id,
                    "status": settled_status,
                    "corrected_keys": sorted(values) if values else [],
                },
                conn=conn,
            )
        if decision == "rerun":
            # The new run is its own execution: starting it outside the decision's
            # transaction keeps one failed start from rolling the review back, and
            # the link below is what makes the pair auditable.
            view = self.start_execution(
                actor,
                workflow_id=execution.workflow_id,
                inputs=rerun_inputs,
                trigger_type="api",
                subject=f"user:{actor.user_id}" if actor.user_id else None,
            )
            self._repo.set_output_review_rerun(ctx, review_id=review.id, rerun_execution_id=view.id)
        row = self._repo.get_output_review(ctx, review.id)
        if row is None:  # pragma: no cover - defensive
            raise _not_found()
        return output_review_payload(row, self._repo.list_output_review_reviewers(ctx, row.id))

    def list_output_reviews(
        self,
        actor: RuntimeActor,
        *,
        scope: str = "self",
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The reviews the caller may read, newest first.

        ``scope="self"`` is the reviewing queue: only what this member was asked to
        review.  ``scope="tenant"`` is the operator view, needs an admin, and is
        audited.
        """
        self._require_postgres()
        if scope not in {"self", "tenant"}:
            raise _invalid("scope must be 'self' or 'tenant'")
        tenant_scope = scope == "tenant"
        if tenant_scope and not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "tenant scope requires tenant admin")
        ctx = self._ctx(actor)
        rows = self._repo.list_output_reviews(
            ctx,
            reviewer_user_id=None if tenant_scope else actor.user_id,
            status=status,
            limit=limit,
        )
        if tenant_scope:
            self._audit(
                actor,
                action="output_review.list_tenant",
                resource_type="output_review",
                resource_id=None,
                details={"count": len(rows)},
            )
        return [output_review_payload(row) for row in rows]

    def list_execution_feedback(
        self, actor: RuntimeActor, execution_id: str
    ) -> list[dict[str, Any]]:
        """What people changed or supplied for one run, oldest first.

        Read by the run's creator or an admin, exactly like the run itself: a
        correction is part of that run's history, not a separate tenant-wide feed.
        The analyser reads the workflow-level slice internally, through the repo.
        """
        self._require_postgres()
        ctx = self._ctx(actor)
        execution = self._load_execution(actor, execution_id)
        rows = self._feedback.list_execution_feedback_for_execution(ctx, execution.id)
        return [feedback_payload(row) for row in rows]

    def attribute_workflow_corrections(
        self,
        actor: RuntimeActor,
        workflow_id: str,
        *,
        since: float | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Corrections for one workflow, grouped by the version they were made against.

        Scoping is per version on purpose: republishing changes the graph, and a
        cluster attributed to the *current* structure would send someone to fix a
        step that is no longer there. Each version's rows are attributed against the
        definition that version actually ran, whose hash is verified on the way in.

        A tenant-admin view, because this is the input to the analyser (A-14), which
        runs as a platform job rather than on behalf of one member.
        """
        self._require_postgres()
        if not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "attribution is a tenant-admin view")
        ctx = self._ctx(actor)
        rows = self._feedback.list_execution_feedback(
            ctx, workflow_id=workflow_id, since=since, limit=limit
        )
        by_version: dict[str, list[ExecutionFeedbackRow]] = {}
        for row in rows:
            by_version.setdefault(row.workflow_version_id, []).append(row)
        versions: list[dict[str, Any]] = []
        for version_id, members in by_version.items():
            locked = self._versions.load_version(ctx, workflow_id, version_id)
            compiled = compile_stored_definition(locked.definition, locked.definition_sha256)
            clusters = attribute_corrections(compiled, members)
            versions.append(
                {
                    "workflow_version_id": version_id,
                    "definition_sha256": locked.definition_sha256,
                    "feedback_rows": len(members),
                    "clusters": [cluster.to_payload() for cluster in clusters],
                }
            )
        versions.sort(key=lambda item: -item["feedback_rows"])
        return {"workflow_id": workflow_id, "versions": versions}

    def _recorded_expired(self, ctx: WorkBuddyDbContext, execution: ExecutionRow) -> frozenset[str]:
        """Nodes of this execution whose question passed its deadline unanswered.

        The worker's sweep has already marked those requests, so the attempt that
        follows fails the node instead of asking the same question a second time.
        """
        rows = self._repo.list_input_requests(ctx, execution_id=execution.id, limit=200)
        return frozenset(row.node_id for row in rows if row.status == "expired")

    def _recorded_decisions(
        self, ctx: WorkBuddyDbContext, execution: ExecutionRow
    ) -> dict[str, str]:
        """Approval decisions already settled for this execution, by node id."""
        rows = self._repo.list_approval_requests(ctx, execution_id=execution.id, limit=200)
        decisions: dict[str, str] = {}
        for row in rows:
            if row.status == "approved":
                decisions[row.node_id] = "approved"
            elif row.status == "rejected":
                decisions[row.node_id] = "rejected"
        return decisions

    def _recorded_answers(
        self, ctx: WorkBuddyDbContext, execution: ExecutionRow
    ) -> dict[str, dict[str, Any]]:
        """Answers already submitted for this execution, by node id.

        Only a submitted request counts: an open one holds no values yet, and an
        invalidated (superseded) one must not be replayed into a fresh attempt.
        """
        rows = self._repo.list_input_requests(ctx, execution_id=execution.id, limit=200)
        answers: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.status == "submitted" and row.values is not None:
                answers[row.node_id] = dict(row.values)
        return answers

    # -- reconciliations ----------------------------------------------------

    def record_reconciliation(
        self,
        actor: RuntimeActor,
        execution_id: str,
        *,
        node_id: str,
        decision: str,
        evidence_ref: str,
        reason: str,
        external_reference: str | None = None,
    ) -> Mapping[str, Any]:
        """Settle an unknown external write from verifiable evidence.

        Only a tenant admin may decide, the decision set is exactly
        ``confirmed_success`` or ``confirmed_failed``, and the evidence must
        already live in this execution's controlled payload store. A success
        backfills the original call's verified result and lets the execution
        continue; a failure terminates that branch. Without evidence there is
        nothing to decide, so the step keeps waiting.
        """
        self._require_postgres()
        if not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "reconciliation requires tenant admin")
        if decision not in RECONCILIATION_DECISIONS:
            raise _invalid("decision must be 'confirmed_success' or 'confirmed_failed'")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise _invalid("a reconciliation needs the operator's reason")
        execution = self._load_execution(actor, execution_id)
        if execution.status != "waiting_reconciliation":
            raise OctopError(
                ErrorCode.STATE_CONFLICT,
                "execution is not waiting for a reconciliation",
            )
        ctx = self._ctx(actor)
        step = self._repo.find_step_run(ctx, execution_id, node_id, status="waiting_reconciliation")
        if step is None:
            raise _not_found()
        graph = self._graph_from_snapshot(execution)
        node = graph.node(node_id)
        if node is None:
            raise _not_found()
        evidence = self._repo.get_payload(ctx, execution_id, evidence_ref)
        if evidence is None:
            raise OctopError(
                ErrorCode.RECONCILIATION_EVIDENCE_INVALID,
                "evidence reference does not belong to this execution",
            )
        evidence_sha, evidence_size = _hash_json(evidence.content)
        if evidence_size > DEFAULT_MAX_OUTPUT_BYTES:
            raise OctopError(
                ErrorCode.RECONCILIATION_EVIDENCE_INVALID,
                "reconciliation evidence exceeds the maximum accepted size",
            )
        result: Any = None
        result_payload_ref: str | None = None
        if decision == "confirmed_success":
            result = self._verified_reconciliation_result(node, evidence.content)
            result_sha, result_size = _hash_json(result)
            if result_size > DEFAULT_MAX_OUTPUT_BYTES:
                raise OctopError(
                    ErrorCode.RECONCILIATION_EVIDENCE_INVALID,
                    "reconciled result exceeds the maximum accepted size",
                )
            result_payload_ref = self._repo.insert_payload(
                ctx,
                tenant_id=actor.tenant_id,
                execution_id=execution_id,
                kind="step_output",
                node_id=node_id,
                content=result,
                sha256=result_sha,
                size_bytes=result_size,
            )
        if not self._repo.settle_step_run(
            ctx,
            step.id,
            status="success" if decision == "confirmed_success" else "failed",
            output=result,
            error_code=None
            if decision == "confirmed_success"
            else "RECONCILIATION_CONFIRMED_FAILED",
            error_message=None if decision == "confirmed_success" else normalized_reason,
        ):
            raise OctopError(
                ErrorCode.STATE_CONFLICT,
                "the parked step was already settled",
            )
        reconciliation_id = self._repo.insert_reconciliation(
            ctx,
            tenant_id=actor.tenant_id,
            execution_id=execution_id,
            step_run_id=step.id,
            decision=decision,
            evidence_ref=evidence_ref,
            evidence_hash=evidence_sha,
            external_request_id=external_reference,
            result_payload_ref=result_payload_ref,
            note=normalized_reason,
            decided_by_user_id=actor.acting_user_id,
        )
        self._audit(
            actor,
            action="reconciliation.record",
            resource_type="execution",
            resource_id=execution_id,
            details={"node_id": node_id, "decision": decision, "evidence_ref": evidence_ref},
        )
        self._repo.insert_notification(
            ctx,
            tenant_id=actor.tenant_id,
            user_id=execution.created_by_user_id or actor.acting_user_id,
            kind="execution.reconciled",
            title=f"External write {decision}",
            body=normalized_reason,
            resource_type="execution",
            resource_id=execution_id,
        )
        # The decision is final; only the recorded result may now advance the DAG.
        if execution.cancel_requested_at is not None:
            with runtime_transaction(self._db, ctx) as conn:
                self._repo.update_execution_status(
                    ctx,
                    execution_id,
                    status="canceled",
                    expected_status=("waiting_reconciliation",),
                    mark_finished=True,
                    conn=conn,
                )
                # The run is terminal, so the slot and the monthly reservation it
                # still holds are settled here; nothing else will settle them.
                self._settle_execution_quota(actor, execution_id, conn=conn, consume=True)
        else:
            # The parked execution holds no running slot, so reconciling it
            # re-queues it: the worker continues the DAG when a slot is free.
            self._repo.requeue_execution(
                ctx,
                execution_id,
                expected_status=("waiting_reconciliation",),
            )
        rows = self._repo.list_reconciliations(ctx, execution_id)
        settled = self._repo.get_execution(ctx, execution_id)
        if settled is None:  # pragma: no cover - defensive
            raise _not_found()
        return {
            "id": reconciliation_id,
            "execution_id": execution_id,
            "node_id": node_id,
            "step_run_id": step.id,
            "decision": decision,
            "external_reference": external_reference,
            "evidence_ref": evidence_ref,
            "evidence_hash": evidence_sha,
            "result_payload_ref": result_payload_ref,
            "reconciliations": len(rows),
            "execution": _execution_view(settled).to_payload(),
        }

    @staticmethod
    def _verified_reconciliation_result(node: GraphNode, evidence: Any) -> Any:
        """The original call's result, checked against the node's output schema.

        The registered tool schema lives in the platform tool catalogue, which is
        not part of this slice yet, so a result is constrained by the node's own
        ``output_schema`` when the definition declares one and is otherwise taken
        as the adapter verified it.
        """
        if not isinstance(evidence, Mapping) or "result" not in evidence:
            raise OctopError(
                ErrorCode.RECONCILIATION_EVIDENCE_INVALID,
                "evidence for a confirmed success must carry the caller's result",
            )
        result = evidence["result"]
        schema = node.config.get("output_schema") if isinstance(node.config, Mapping) else None
        if isinstance(schema, Mapping):
            import jsonschema

            try:
                jsonschema.validate(result, schema)
            except jsonschema.ValidationError as exc:
                raise OctopError(
                    ErrorCode.RECONCILIATION_EVIDENCE_INVALID,
                    f"reconciled result does not match the tool output schema: {exc.message}",
                ) from exc
        return result

    def list_reconciliations(
        self, actor: RuntimeActor, execution_id: str
    ) -> list[Mapping[str, Any]]:
        self._require_postgres()
        self._load_execution(actor, execution_id)
        rows: list[ReconciliationRow] = self._repo.list_reconciliations(
            self._ctx(actor), execution_id
        )
        return [
            {
                "id": row.id,
                "step_run_id": row.step_run_id,
                "decision": row.decision,
                "evidence_ref": row.evidence_ref,
                "evidence_hash": row.evidence_hash,
                "external_request_id": row.external_request_id,
                "result_payload_ref": row.result_payload_ref,
                "note": row.note,
                "decided_by_user_id": row.decided_by_user_id,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]

    # -- approvals ----------------------------------------------------------

    def list_approval_requests(
        self,
        actor: RuntimeActor,
        *,
        status: str | None = None,
        scope: str = "self",
        limit: int = 50,
    ) -> list[ApprovalView]:
        self._require_postgres()
        tenant_scope = scope == "tenant"
        if tenant_scope and not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "tenant scope requires tenant admin")
        rows = self._repo.list_approval_requests(
            self._ctx(actor),
            candidate_user_id=None if tenant_scope else actor.user_id,
            status=status,
            limit=limit,
        )
        return [_approval_view(row) for row in rows]

    def get_approval_request(self, actor: RuntimeActor, approval_request_id: str) -> ApprovalView:
        self._require_postgres()
        return _approval_view(self._load_approval(actor, approval_request_id))

    def approval_candidates(
        self, actor: RuntimeActor, approval_request_id: str
    ) -> list[ApprovalCandidateRow]:
        self._require_postgres()
        self._load_approval(actor, approval_request_id)
        return self._repo.list_approval_candidates(self._ctx(actor), approval_request_id)

    # -- jobs ---------------------------------------------------------------

    def create_job(
        self,
        actor: RuntimeActor,
        *,
        kind: str,
        execution_id: str | None = None,
        idempotency_key: str | None = None,
        request: Mapping[str, Any] | None = None,
    ) -> JobView:
        """Record an asynchronous job fact owned by another slice."""
        self._require_postgres()
        self._assert_tenant_active(actor)
        request_hash = _hash_json(dict(request or {}))[0]
        if idempotency_key:
            existing = self._repo.get_job_by_idempotency(
                self._ctx(actor),
                tenant_id=actor.tenant_id,
                kind=kind,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OctopError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was already used with a different request",
                    )
                return _job_view(existing)
        job_id = self._repo.insert_job(
            self._ctx(actor),
            tenant_id=actor.tenant_id,
            kind=kind,
            requested_by_user_id=actor.acting_user_id,
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        self._audit(
            actor,
            action="job.create",
            resource_type="job",
            resource_id=job_id,
            details={"kind": kind},
        )
        row = self._repo.get_job(self._ctx(actor), job_id)
        if row is None:
            raise _not_found()
        return _job_view(row)

    def complete_job(
        self,
        actor: RuntimeActor,
        job_id: str,
        *,
        status: str,
        progress: int = 100,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JobView:
        """Settlement of a job fact; allowed while the tenant is suspended."""
        self._require_postgres()
        if status not in {"success", "failed", "canceled"}:
            raise _invalid("job completion status is not supported")
        ctx = self._ctx(actor)
        self._repo.finish_job(
            ctx,
            job_id,
            status=status,
            progress=progress,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )
        row = self._repo.get_job(ctx, job_id)
        if row is None:
            raise _not_found()
        return _job_view(row)

    def list_jobs(
        self, actor: RuntimeActor, *, status: str | None = None, limit: int = 50
    ) -> list[JobView]:
        self._require_postgres()
        rows = self._repo.list_jobs(
            self._ctx(actor), requested_by_user_id=actor.acting_user_id, status=status, limit=limit
        )
        return [_job_view(row) for row in rows]

    def get_job(self, actor: RuntimeActor, job_id: str) -> JobView:
        self._require_postgres()
        ctx = self._ctx(actor)
        row = self._repo.get_job(ctx, job_id)
        if row is None or (not actor.is_admin and row.requested_by_user_id != actor.user_id):
            raise _not_found()
        return _job_view(row)

    # -- notifications ------------------------------------------------------

    def list_notifications(
        self, actor: RuntimeActor, *, unread_only: bool = False, limit: int = 50
    ) -> list[NotificationView]:
        self._require_postgres()
        rows = self._repo.list_notifications(
            self._ctx(actor), user_id=actor.acting_user_id, unread_only=unread_only, limit=limit
        )
        return [_notification_view(row) for row in rows]

    def mark_notification_read(self, actor: RuntimeActor, notification_id: str) -> NotificationView:
        self._require_postgres()
        ctx = self._ctx(actor)
        if not self._repo.mark_notification_read(
            ctx, notification_id, user_id=actor.acting_user_id
        ):
            rows = self._repo.list_notifications(ctx, user_id=actor.acting_user_id, limit=200)
            existing = next((row for row in rows if row.id == notification_id), None)
            if existing is None:
                raise _not_found()
        rows = self._repo.list_notifications(ctx, user_id=actor.acting_user_id, limit=200)
        for row in rows:
            if row.id == notification_id:
                return _notification_view(row)
        raise _not_found()

    # -- private chat -------------------------------------------------------

    def chat(
        self,
        actor: RuntimeActor,
        *,
        message: str,
        session_id: str | None = None,
    ) -> ChatSessionView:
        self._require_postgres()
        self._assert_tenant_active(actor)
        content = message.strip()
        if not content:
            raise _invalid("chat message must not be empty")
        ctx = self._ctx(actor)
        if session_id is None:
            session_id = self._repo.create_chat_session(
                ctx, tenant_id=actor.tenant_id, user_id=actor.acting_user_id, title=content[:80]
            )
        else:
            session = self._repo.get_chat_session(ctx, session_id)
            if session is None or (not actor.is_admin and session.user_id != actor.user_id):
                raise _not_found()
        history = self._repo.list_chat_messages(ctx, session_id)
        self._repo.insert_chat_message(
            ctx,
            tenant_id=actor.tenant_id,
            session_id=session_id,
            role="user",
            content=content,
        )
        reply = self._effects.respond_chat(session_id=session_id, message=content, history=history)
        self._repo.insert_chat_message(
            ctx,
            tenant_id=actor.tenant_id,
            session_id=session_id,
            role="assistant",
            content=reply.content,
            model_revision=reply.model_revision,
            usage=dict(reply.usage or {}) or None,
        )
        self._audit(
            actor,
            action="chat.message",
            resource_type="chat_session",
            resource_id=session_id,
        )
        session_row = self._repo.get_chat_session(ctx, session_id)
        if session_row is None:
            raise _not_found()
        return _chat_session_view(session_row, self._repo.list_chat_messages(ctx, session_id))

    def list_chat_sessions(self, actor: RuntimeActor, *, limit: int = 50) -> list[ChatSessionView]:
        self._require_postgres()
        rows = self._repo.list_chat_sessions(
            self._ctx(actor), user_id=actor.acting_user_id, limit=limit
        )
        return [_chat_session_view(row) for row in rows]

    def get_chat_session(self, actor: RuntimeActor, session_id: str) -> ChatSessionView:
        self._require_postgres()
        ctx = self._ctx(actor)
        row = self._repo.get_chat_session(ctx, session_id)
        if row is None or (not actor.is_admin and row.user_id != actor.user_id):
            raise _not_found()
        return _chat_session_view(row, self._repo.list_chat_messages(ctx, session_id))

    # -- audit and usage ----------------------------------------------------

    def list_audit_logs(
        self,
        actor: RuntimeActor,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 50,
    ) -> list[AuditLogView]:
        self._require_postgres()
        if not actor.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "audit logs require tenant admin")
        rows = self._repo.list_audit_logs(
            self._ctx(actor), action=action, resource_type=resource_type, limit=limit
        )
        return [_audit_view(row) for row in rows]

    def usage(self, actor: RuntimeActor, *, days: int = 30) -> Mapping[str, Any]:
        self._require_postgres()
        ctx = self._ctx(actor)
        window_days = max(1, min(int(days), 365))
        since = datetime.now(UTC) - timedelta(days=window_days)
        by_status = self._repo.execution_counts(
            ctx,
            tenant_id=actor.tenant_id,
            created_by_user_id=None if actor.is_admin else actor.user_id,
            since=since,
        )
        jobs = self._repo.list_jobs(
            ctx,
            requested_by_user_id=None if actor.is_admin else actor.user_id,
            limit=200,
        )
        return {
            "window_days": window_days,
            "tenant_id": actor.tenant_id,
            "scope": "tenant" if actor.is_admin else "self",
            "executions": {
                "total": sum(by_status.values()),
                "by_status": by_status,
            },
            "jobs": {"total": len(jobs)},
            "quota_usage": [
                {
                    "quota_key": key,
                    "amount": self._repo.quota_usage_total(
                        ctx, tenant_id=actor.tenant_id, quota_key=key, since=since
                    ),
                }
                for key in ("executions", "tokens")
            ],
        }


__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_TOKEN_TTL_SECONDS",
    "ApprovalView",
    "AuditLogView",
    "ChatReply",
    "ChatSessionView",
    "EdgeOutcome",
    "ExecutionView",
    "GraphEdge",
    "GraphNode",
    "GraphRun",
    "JobView",
    "LockedWorkflowVersion",
    "NODE_TYPES",
    "NotificationView",
    "ReplayState",
    "RuntimeActor",
    "SideEffectPort",
    "StepOutcome",
    "StepRunView",
    "UNAVAILABLE_SIDE_EFFECTS",
    "UNAVAILABLE_WORKFLOW_VERSIONS",
    "UnavailableSideEffects",
    "UnavailableWorkflowVersions",
    "WorkBuddyRuntimeService",
    "WorkflowGraph",
    "WorkflowCatalogVersions",
    "WorkflowVersionSource",
    "approval_requirements",
    "compile_locked_definition",
    "graph_from_compiled",
    "run_graph",
]
