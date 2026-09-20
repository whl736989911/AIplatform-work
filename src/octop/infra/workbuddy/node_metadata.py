"""Node, field and reference metadata for the workflow editor, schema-derived.

An editor that renders a form for a definition needs exactly three things the
compiler already owns: which fields a node type carries and which of them are
required, what those fields accept, and the grammar of the values that point at
other data (templates, references, CEL namespaces).  Restating any of that here
would create a second contract that drifts from the compiler silently, so this
module *derives* the document from the two sources that already bind the
platform — :func:`~octop.infra.workbuddy.workflow_compiler.workflow_schema`
(``contracts/workflow-v1.schema.json``) and the compiler's own constants — and
keeps no field list of its own.

A checkout whose schema and compiler disagree is unusable: the document is
refused as unavailable rather than served with a field set the compiler does not
enforce.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from octop.infra.workbuddy.workflow_compiler import (
    CEL_REFERENCE_NAMESPACES,
    MAX_REFERENCE_LENGTH,
    MAX_TEMPLATE_PLACEHOLDERS,
    NODE_TYPES,
    REFERENCE_SYNTAX,
    TEMPLATE_FIELD_PATHS,
    WORKFLOW_COMPILER_VERSION,
    WORKFLOW_DEPENDENCY_UNAVAILABLE,
    WORKFLOW_SCHEMA_VERSION,
    WorkflowCompileError,
    workflow_schema,
)

#: Schema keywords a form renderer needs; anything else stays out of the contract.
_CONSTRAINT_KEYS = (
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "maxProperties",
    "minProperties",
    "pattern",
    "format",
    "default",
)


def _resolved(schema: Mapping[str, Any], spec: Any) -> Mapping[str, Any]:
    """Resolve a local ``$ref`` chain; the schema allows nothing else."""
    current = spec
    for _ in range(8):
        if not isinstance(current, Mapping):
            return {}
        reference = current.get("$ref")
        if not isinstance(reference, str):
            return current
        current = (schema.get("definitions") or {}).get(reference.rsplit("/", 1)[-1])
    raise WorkflowCompileError(
        WORKFLOW_DEPENDENCY_UNAVAILABLE, "workflow schema has an unresolvable reference chain"
    )


def _definition(schema: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    definitions = schema.get("definitions") or {}
    if name not in definitions:
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE, f"workflow schema lacks the {name!r} definition"
        )
    return _resolved(schema, definitions[name])


def _field(schema: Mapping[str, Any], name: str, spec: Any, *, required: bool) -> dict[str, Any]:
    """One form field: name, JSON type, whether it is required, and its bounds."""
    resolved = _resolved(schema, spec)
    field: dict[str, Any] = {
        "name": name,
        "type": resolved.get("type", "any"),
        "required": required,
    }
    for key in _CONSTRAINT_KEYS:
        if key in resolved:
            field[key] = resolved[key]
    items = resolved.get("items")
    if isinstance(items, Mapping):
        item = _resolved(schema, items)
        field["items"] = {
            key: value for key, value in item.items() if key in ("type", *_CONSTRAINT_KEYS)
        }
    properties = resolved.get("properties")
    if isinstance(properties, Mapping):
        nested_required = set(resolved.get("required") or ())
        field["fields"] = [
            _field(schema, child, child_spec, required=child in nested_required)
            for child, child_spec in properties.items()
        ]
    return field


def _block(
    schema: Mapping[str, Any], properties: Mapping[str, Any], required: Any
) -> dict[str, Any]:
    """One ``properties``/``required`` pair as required names, optional names, descriptors."""
    required_names = [name for name in required or () if name in properties]
    optional_names = sorted(set(properties) - set(required_names))
    return {
        "required": required_names,
        "optional": optional_names,
        "fields": [
            _field(schema, name, spec, required=name in required_names)
            for name, spec in properties.items()
        ],
    }


def _declaration(schema: Mapping[str, Any], name: str) -> dict[str, Any]:
    """One top-level or ``definitions`` block rendered as a field block."""
    definition = _definition(schema, name) if name in (schema.get("definitions") or {}) else {}
    return _block(schema, definition.get("properties") or {}, definition.get("required"))


def _branches(
    schema: Mapping[str, Any], definition_name: str, *, discriminant: str = "type"
) -> list[tuple[str, Mapping[str, Any]]]:
    """Every ``if <discriminant> == const`` branch of one definition.

    The schema discriminates five node types and four trigger types this way, so
    the walk is written once instead of one hand-maintained table per family.
    """
    definition = _definition(schema, definition_name)
    found: list[tuple[str, Mapping[str, Any]]] = []
    for branch in definition.get("allOf") or ():
        condition = (branch.get("if") or {}).get("properties") or {}
        const = (condition.get(discriminant) or {}).get("const")
        if not isinstance(const, str):
            continue
        found.append((const, (branch.get("then") or {}).get("properties") or {}))
    return found


def _node_types(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every node type with its config contract, derived from the schema branches."""
    types: list[dict[str, Any]] = []
    for node_type, then_properties in _branches(schema, "node"):
        config = _resolved(schema, then_properties.get("config"))
        block = _block(schema, config.get("properties") or {}, config.get("required"))
        template_field = TEMPLATE_FIELD_PATHS.get(node_type)
        types.append(
            {
                "type": node_type,
                "required": block["required"],
                "optional": block["optional"],
                "config_fields": block["fields"],
                "template_fields": [template_field] if template_field else [],
            }
        )
    types.sort(key=lambda item: item["type"])
    if {item["type"] for item in types} != set(NODE_TYPES):
        raise WorkflowCompileError(
            WORKFLOW_DEPENDENCY_UNAVAILABLE,
            "workflow schema node types do not match the compiler's node types",
        )
    return types


def _trigger_types(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every trigger type, with the config keys the schema requires of it."""
    trigger = _definition(schema, "trigger")
    declarations = trigger.get("properties") or {}
    default_config = _resolved(schema, declarations.get("config"))
    configs = {
        trigger_type: _resolved(schema, then_properties.get("config"))
        for trigger_type, then_properties in _branches(schema, "trigger")
    }
    types: list[dict[str, Any]] = []
    for trigger_type in _resolved(schema, declarations.get("type")).get("enum") or []:
        config = configs.get(trigger_type, default_config)
        block = _block(schema, config.get("properties") or {}, config.get("required"))
        types.append({"type": trigger_type, **block})
    return types


def _reference_syntax(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The value grammar a definition may use to point at other data."""
    identifier = _definition(schema, "identifier")
    template_examples = [f"{{{{ {syntax} }}}}" for syntax in REFERENCE_SYNTAX.values()]
    return {
        "identifier": {"pattern": identifier.get("pattern")},
        "template": {
            "open": "{{",
            "close": "}}",
            "examples": template_examples,
            "max_placeholders": MAX_TEMPLATE_PLACEHOLDERS,
            "fields": dict(TEMPLATE_FIELD_PATHS),
        },
        "references": [
            {"kind": kind, "syntax": syntax} for kind, syntax in REFERENCE_SYNTAX.items()
        ],
        "max_reference_length": MAX_REFERENCE_LENGTH,
    }


def definition_metadata() -> dict[str, Any]:
    """The derived contract an editor renders definition and node forms from.

    Every node type, required field, bound and reference form in the result comes
    from the checked-in schema or from the compiler's own constants; nothing here
    is a second copy of either, and a node type the schema does not lay out is
    refused instead of being invented.
    """
    schema = workflow_schema()
    properties = schema.get("properties") or {}
    inputs = _declaration(schema, "inputDeclaration")
    input_type_field: Mapping[str, Any] = next(
        (field for field in inputs["fields"] if field["name"] == "type"), {"enum": []}
    )
    edges = (properties.get("edges") or {}).get("items") or {}
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "compiler_version": WORKFLOW_COMPILER_VERSION,
        "node_types": _node_types(schema),
        "node": _declaration(schema, "node"),
        "inputs": {"types": input_type_field.get("enum") or [], **inputs},
        "edges": _block(schema, edges.get("properties") or {}, edges.get("required")),
        "trigger_types": _trigger_types(schema),
        "limits": _block(schema, (properties.get("limits") or {}).get("properties") or {}, None),
        "output": _block(
            schema,
            _resolved(schema, properties.get("output")).get("properties") or {},
            _resolved(schema, properties.get("output")).get("required"),
        ),
        "reference_syntax": _reference_syntax(schema),
        "cel_reference_namespaces": [
            {"namespace": namespace, "kind": kind, "syntax": syntax}
            for namespace, (kind, syntax) in CEL_REFERENCE_NAMESPACES.items()
        ],
    }


__all__ = ["definition_metadata"]
