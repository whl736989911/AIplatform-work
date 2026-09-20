"""Authoring a workflow from a description (A-15).

The rule this module exists to honour is the product contract's own: the runtime
only ever executes a definition the compiler accepted, and nothing a model writes
is executed directly.  So the model's job here is deliberately *not* to produce a
definition.  It produces a **restricted authoring document** — a small, flat shape
whose vocabulary is derived from the schema-derived metadata — which this module
checks and then lowers, deterministically, into a definition.

Two consequences worth stating plainly:

* A model cannot smuggle structure in.  Step ids are checked, a step may only use
  steps declared before it (so the graph is acyclic by construction), each kind
  must exist in the metadata, and a config key that the schema does not declare for
  that kind is refused — the document is where the restriction lives.
* The compiler has the last word.  If it refuses, its own diagnostics go back to
  the model for one more attempt; after ``max_rounds`` the attempt is reported as
  failed, carrying the diagnostics, instead of storing something unproven.

References in a document are written ``{{ steps.<id>... }}`` and lowered to the
definition's own ``{{ nodes.<id>... }}`` namespace: one mechanical translation, so
the shape the model writes is never the shape the runtime reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from octop.infra.workbuddy.node_metadata import definition_metadata
from octop.infra.workbuddy.workflow_compiler import (
    WorkflowCompileError,
    compile_workflow_definition,
)

#: Authoring rounds allowed before an attempt is reported as failed.  The
#: accepted behaviour is "a description compiles within two rounds": one draft plus
#: one repair.  A third round means the model is guessing rather than reading the
#: diagnostics, and a person's time is worth more than the tokens.
MAX_AUTHORING_ROUNDS = 2

#: Step ids double as definition node ids, so they must satisfy the schema's
#: identifier pattern rather than merely being unique.
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: The reference namespace inside a document.  ``steps`` is written by the author,
#: ``nodes`` is what the compiler understands; the lowering translates between them.
_AUTHOR_REFERENCE = "{{ steps."
_DEFINITION_REFERENCE = "{{ nodes."


@dataclass(frozen=True, slots=True)
class AuthoringDiagnostic:
    """One reason a document (or the definition it lowered to) was not accepted."""

    code: str
    message: str
    path: str = ""
    step_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "step_id": self.step_id,
        }


@dataclass(frozen=True, slots=True)
class AuthoringStep:
    """A step as the author described it, for the explanation returned to a person."""

    id: str
    kind: str
    purpose: str
    uses: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "purpose": self.purpose, "uses": list(self.uses)}


@dataclass(frozen=True, slots=True)
class AuthoringOutcome:
    """What came out of the loop: a compilable definition, or why there is none.

    ``ok`` is the only thing a caller must branch on.  When it is true the
    definition is known to compile and the run of the loop is explained by ``steps``
    and ``rounds``; when it is false ``diagnostics`` says what the last attempt
    still got wrong, and the definition is deliberately *not* offered for storage.
    """

    ok: bool
    definition: Mapping[str, Any] | None
    steps: tuple[AuthoringStep, ...]
    rounds: int
    diagnostics: tuple[AuthoringDiagnostic, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rounds": self.rounds,
            "steps": [step.to_payload() for step in self.steps],
            "diagnostics": [item.to_payload() for item in self.diagnostics],
            "definition": dict(self.definition) if self.definition is not None else None,
        }


class AuthoringSource(Protocol):
    """Whoever writes the authoring document: a model adapter, or a test double."""

    def draft(
        self,
        *,
        request: str,
        metadata: Mapping[str, Any],
        existing: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]: ...

    def revise(
        self,
        *,
        document: Mapping[str, Any],
        diagnostics: Sequence[AuthoringDiagnostic],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def _config_fields(metadata: Mapping[str, Any], kind: str) -> tuple[set[str], set[str]] | None:
    """Declared required/optional config keys for one node kind, or ``None``."""
    for node_type in metadata.get("node_types") or ():
        if node_type.get("type") == kind:
            return set(node_type.get("required") or ()), set(node_type.get("optional") or ())
    return None


def _template_references(value: Any) -> list[str]:
    """Every ``{{ steps.<id>... }}`` target mentioned anywhere in a config value."""
    found: list[str] = []
    if isinstance(value, str):
        for match in re.finditer(r"\{\{\s*steps\.([A-Za-z0-9_]+)", value):
            found.append(match.group(1))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_template_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_template_references(item))
    return found


def validate_authoring(
    document: Any, *, metadata: Mapping[str, Any] | None = None
) -> tuple[AuthoringDiagnostic, ...]:
    """Check a document against the derived vocabulary before anything is lowered.

    These are the refusals that keep the model's shape restricted: unknown kinds,
    undeclared config keys, duplicate ids, forward or dangling references.  Only a
    document that passes here is worth compiling, so the compiler's diagnostics
    stay about the workflow rather than about the authoring format.
    """
    meta = metadata if metadata is not None else definition_metadata()
    problems: list[AuthoringDiagnostic] = []
    if not isinstance(document, Mapping):
        return (AuthoringDiagnostic("AUTHORING_SHAPE", "the document must be a JSON object"),)
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        return (
            AuthoringDiagnostic("AUTHORING_SHAPE", "the document needs a non-empty steps list"),
        )

    declared_inputs = {
        str(item.get("key"))
        for item in document.get("inputs") or ()
        if isinstance(item, Mapping) and item.get("key")
    }
    seen: list[str] = []
    for index, step in enumerate(steps):
        path = f"/steps/{index}"
        if not isinstance(step, Mapping):
            problems.append(
                AuthoringDiagnostic("AUTHORING_SHAPE", "a step must be an object", path)
            )
            continue
        step_id = str(step.get("id") or "")
        if not STEP_ID_PATTERN.match(step_id):
            problems.append(
                AuthoringDiagnostic(
                    "AUTHORING_STEP_ID",
                    "a step id must be lower_snake_case and start with a letter",
                    path,
                    step_id or None,
                )
            )
        elif step_id in seen:
            problems.append(
                AuthoringDiagnostic("AUTHORING_STEP_ID", "step ids must be unique", path, step_id)
            )
        kind = str(step.get("kind") or "")
        declared = _config_fields(meta, kind)
        if declared is None:
            problems.append(
                AuthoringDiagnostic(
                    "AUTHORING_NODE_KIND",
                    f"{kind!r} is not a node kind this schema declares",
                    path,
                    step_id or None,
                )
            )
        uses = step.get("uses") or []
        if not isinstance(uses, list) or any(not isinstance(item, str) for item in uses):
            problems.append(
                AuthoringDiagnostic(
                    "AUTHORING_SHAPE", "uses must be a list of step ids", path, step_id or None
                )
            )
            uses = []
        for used in uses:
            if used not in seen:
                # Forward references are how a cycle would be expressible, so the
                # document's own order is the topological order and is enforced.
                problems.append(
                    AuthoringDiagnostic(
                        "AUTHORING_STEP_ORDER",
                        f"step {step_id!r} uses {used!r}, which is not an earlier step",
                        path,
                        step_id or None,
                    )
                )
        config = step.get("config") or {}
        if not isinstance(config, Mapping):
            problems.append(
                AuthoringDiagnostic(
                    "AUTHORING_SHAPE", "config must be an object", path, step_id or None
                )
            )
        elif declared is not None:
            required, optional = declared
            unknown = sorted(set(config) - required - optional)
            for name in unknown:
                problems.append(
                    AuthoringDiagnostic(
                        "AUTHORING_CONFIG_FIELD",
                        f"{kind!r} has no config field {name!r}",
                        f"{path}/config/{name}",
                        step_id or None,
                    )
                )
            for name in sorted(required - set(config)):
                problems.append(
                    AuthoringDiagnostic(
                        "AUTHORING_CONFIG_REQUIRED",
                        f"{kind!r} requires config field {name!r}",
                        f"{path}/config/{name}",
                        step_id or None,
                    )
                )
        for target in _template_references(config):
            if target not in seen and target not in declared_inputs:
                problems.append(
                    AuthoringDiagnostic(
                        "AUTHORING_REFERENCE",
                        f"{{{{ steps.{target} }}}} does not name an earlier step or a declared input",
                        path,
                        step_id or None,
                    )
                )
        if step_id:
            seen.append(step_id)
    return tuple(problems)


def _lower_config(value: Any) -> Any:
    """Rewrite author references into the definition's namespace, mechanically."""
    if isinstance(value, str):
        return value.replace(_AUTHOR_REFERENCE, _DEFINITION_REFERENCE)
    if isinstance(value, Mapping):
        return {str(key): _lower_config(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_lower_config(item) for item in value]
    return value


def lower_authoring(document: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a validated document into a definition the compiler can judge.

    Deterministic and total: the same document always lowers to the same
    definition, and every step becomes a node whose ``save_as`` is its id, so a
    later reference and the run's result keys stay the author's own names.
    """
    steps = document.get("steps") or []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for step in steps:
        step_id = str(step["id"])
        uses = [str(item) for item in step.get("uses") or ()]
        nodes.append(
            {
                "id": step_id,
                "type": str(step["kind"]),
                "name": str(step.get("name") or step.get("purpose") or step_id)[:200],
                "config": _lower_config(step.get("config") or {}),
                "save_as": step_id,
            }
        )
        edges.extend({"from": used, "to": step_id} for used in uses)

    trigger = document.get("trigger") or {"type": "manual"}
    if isinstance(trigger, str):
        trigger = {"type": trigger}
    definition: dict[str, Any] = {
        "schema_version": int(document.get("schema_version") or 1),
        "trigger": {
            "type": str(trigger.get("type") or "manual"),
            "config": dict(trigger.get("config") or {}),
        },
        "inputs": {
            str(item["key"]): {
                key: value
                for key, value in item.items()
                if key in {"type", "required", "default", "description"} and value is not None
            }
            for item in document.get("inputs") or ()
        },
        "nodes": nodes,
        "edges": edges,
    }
    # ``name``/``description`` stay out: the schema forbids extra top-level keys,
    # and a workflow's name belongs to its record rather than to its definition.
    return definition


def _steps_of(document: Mapping[str, Any]) -> tuple[AuthoringStep, ...]:
    return tuple(
        AuthoringStep(
            id=str(step.get("id") or ""),
            kind=str(step.get("kind") or ""),
            purpose=str(step.get("purpose") or ""),
            uses=tuple(str(item) for item in step.get("uses") or ()),
        )
        for step in document.get("steps") or ()
        if isinstance(step, Mapping)
    )


def author_workflow(
    *,
    request: str,
    source: AuthoringSource,
    existing: Mapping[str, Any] | None = None,
    max_rounds: int = MAX_AUTHORING_ROUNDS,
    metadata: Mapping[str, Any] | None = None,
) -> AuthoringOutcome:
    """Draft, check and compile — feeding the failures back, at most ``max_rounds`` times.

    The loop only ever hands the model *diagnostics*, never the compiled result: the
    question each repair round answers is "what does the compiler still object to",
    and the answer is the same object a person would read in the editor.
    """
    meta = metadata if metadata is not None else definition_metadata()
    document: Mapping[str, Any] = source.draft(request=request, metadata=meta, existing=existing)
    diagnostics: tuple[AuthoringDiagnostic, ...] = ()
    rounds = 1
    while True:
        problems = validate_authoring(document, metadata=meta)
        definition: Mapping[str, Any] | None = None
        if not problems:
            definition = lower_authoring(document)
            try:
                compile_workflow_definition(definition)
            except WorkflowCompileError as exc:
                diagnostics = tuple(
                    AuthoringDiagnostic(
                        code=str(item.code),
                        message=str(item.message),
                        path=str(getattr(item, "path", "") or ""),
                        step_id=getattr(item, "node_id", None),
                    )
                    for item in (exc.diagnostics or ())
                ) or (AuthoringDiagnostic(str(exc.code), str(exc)),)
            else:
                return AuthoringOutcome(
                    ok=True,
                    definition=definition,
                    steps=_steps_of(document),
                    rounds=rounds,
                )
        else:
            diagnostics = problems
        if rounds >= max_rounds:
            return AuthoringOutcome(
                ok=False,
                definition=None,
                steps=_steps_of(document),
                rounds=rounds,
                diagnostics=diagnostics,
            )
        rounds += 1
        document = source.revise(document=document, diagnostics=diagnostics, metadata=meta)


__all__ = [
    "MAX_AUTHORING_ROUNDS",
    "STEP_ID_PATTERN",
    "AuthoringDiagnostic",
    "AuthoringOutcome",
    "AuthoringSource",
    "AuthoringStep",
    "author_workflow",
    "lower_authoring",
    "validate_authoring",
]
