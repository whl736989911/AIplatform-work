"""Focused tests for the WorkBuddy workflow compiler (schema, DAG, CEL, semantics)."""

from __future__ import annotations

import json

import pytest

from octop.infra.workbuddy.workflow_compiler import (
    WORKFLOW_APPROVER_INVALID,
    WORKFLOW_CONDITION_EDGES,
    WORKFLOW_CYCLE,
    WORKFLOW_DEPENDENCY_UNAVAILABLE,
    WORKFLOW_EDGE_DUPLICATE,
    WORKFLOW_EDGE_SELF_LOOP,
    WORKFLOW_EDGE_UNKNOWN_NODE,
    WORKFLOW_ENTRY_COUNT,
    WORKFLOW_KNOWLEDGE_BASE_UNKNOWN,
    WORKFLOW_MODEL_NOT_CONFIGURED,
    WORKFLOW_NODE_DUPLICATE_ID,
    WORKFLOW_REFERENCE_NOT_UPSTREAM,
    WORKFLOW_REFERENCE_UNKNOWN,
    WORKFLOW_SAVE_AS_DUPLICATE,
    WORKFLOW_SCHEMA_INVALID,
    WORKFLOW_TEMPLATE_INVALID,
    WORKFLOW_TOOL_UNAVAILABLE,
    WORKFLOW_VERSION_HASH_MISMATCH,
    SemanticDecision,
    WorkflowCompileError,
    canonical_definition_json,
    compile_stored_definition,
    compile_workflow_definition,
    definition_sha256,
    normalize_definition,
    parse_reference,
    verify_definition_hash,
)

APPROVER = "3f1c1f1e-2a30-4f8c-9f0d-6c2c5b1a9d10"


def manual_trigger() -> dict:
    return {"type": "manual", "config": {}}


def transform(
    name: str, expression: str, *, config: dict | None = None, save_as: str | None = None
) -> dict:
    node = {
        "id": name,
        "type": "transform",
        "name": name,
        "config": {"input": {}, "expression": expression, **(config or {})},
    }
    if save_as is not None:
        node["save_as"] = save_as
    return node


def hello_definition() -> dict:
    """The minimal "hello" example: one transform, one output node."""
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "inputs": {
            "who": {"type": "string", "required": True, "default": "world"},
        },
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {
                    "input": {"greeting": "hello {{ inputs.who }}"},
                    "expression": "inputs",
                },
                "save_as": "greeting",
            },
        ],
        "edges": [],
    }


def condition_definition() -> dict:
    """A branch: fetch -> decide -> (true: greet | false: farewell)."""
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "inputs": {"farewell": {"type": "string", "required": False}},
        "nodes": [
            transform(
                "fetch",
                "'hello world'",
                config={"input": {"seed": "hello world"}},
                save_as="fetched",
            ),
            {
                "id": "decide",
                "type": "condition",
                "name": "Greeting matched",
                "config": {"expression": 'outputs.fetched == "hello world"'},
            },
            transform(
                "greet",
                "outputs.fetched",
                config={"input": {"text": "{{ nodes.fetch.output }}"}},
                save_as="greeting",
            ),
            transform(
                "farewell",
                "inputs.farewell",
                config={"input": {"text": "{{ inputs.farewell }}"}},
                save_as="farewell_text",
            ),
        ],
        "edges": [
            {"from": "fetch", "to": "decide"},
            {"from": "decide", "to": "greet", "when": "true"},
            {"from": "decide", "to": "farewell", "when": "false"},
        ],
    }


def test_hello_example_compiles_with_defaults_and_canonical_hash() -> None:
    compiled = compile_workflow_definition(hello_definition())

    assert compiled.entry_node_id == "hello"
    assert compiled.exit_node_ids == ("hello",)
    assert compiled.topological_order == ("hello",)
    assert compiled.save_as_by_node == {"hello": "greeting"}
    assert compiled.definition["output"] == {"format": "json", "destination": "user"}
    assert compiled.definition["limits"]["max_steps"] == 50
    assert compiled.definition["inputs"]["who"]["required"] is True
    assert compiled.definition_sha256 == definition_sha256(compiled.definition)
    assert compiled.semantic_checks == "not_required"


def test_condition_example_compiles_and_records_cel_evidence() -> None:
    compiled = compile_workflow_definition(condition_definition())

    decide = compiled.node("decide")
    assert [edge.to_node_id for edge in decide.outgoing] == ["farewell", "greet"]
    assert sorted(edge.when for edge in decide.outgoing) == ["false", "true"]
    assert compiled.entry_node_id == "fetch"
    assert compiled.node("greet").upstream_node_ids == {"fetch", "decide"}
    assert "nodes.decide.config.expression" in compiled.cel_evidence


def test_normalization_is_idempotent_and_order_independent() -> None:
    first = normalize_definition(condition_definition())
    second = normalize_definition(first)
    shuffled = dict(condition_definition())
    shuffled["nodes"] = list(reversed(shuffled["nodes"]))
    shuffled["edges"] = list(reversed(shuffled["edges"]))

    assert first == second
    assert definition_sha256(first) == definition_sha256(normalize_definition(shuffled))
    assert json.loads(canonical_definition_json(first)) == first


def test_schema_rejects_unknown_property_and_bad_uuid_format() -> None:
    extra = hello_definition()
    extra["nodes"][0]["surprise"] = True
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(extra)
    assert caught.value.code == WORKFLOW_SCHEMA_INVALID

    bad_uuid = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "approve",
                "type": "approval",
                "name": "Approve",
                "config": {
                    "approval_message": "ok?",
                    "approver_user_ids": ["not-a-uuid"],
                },
            }
        ],
        "edges": [],
    }
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(bad_uuid)
    assert caught.value.code == WORKFLOW_SCHEMA_INVALID


def two_node_definition() -> dict:
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "inputs": {"seed": {"type": "string"}},
        "nodes": [
            transform(
                "alpha",
                "inputs.seed",
                config={"input": {"seed": "{{ inputs.seed }}"}},
                save_as="first",
            ),
            transform(
                "beta",
                "'done'",
                config={"input": {"value": "{{ nodes.alpha.output }}"}},
                save_as="second",
            ),
        ],
        "edges": [{"from": "alpha", "to": "beta"}],
    }


def branch_definition() -> dict:
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            transform("alpha", "'seed'", save_as="first"),
            {
                "id": "route",
                "type": "condition",
                "name": "Route",
                "config": {"expression": "outputs.first != ''"},
            },
            transform("left", "'left'", save_as="left_out"),
            transform("right", "'right'", save_as="right_out"),
        ],
        "edges": [
            {"from": "alpha", "to": "route"},
            {"from": "route", "to": "left", "when": "true"},
            {"from": "route", "to": "right", "when": "false"},
        ],
    }


def _duplicate_node_id() -> dict:
    definition = two_node_definition()
    definition["nodes"].append(dict(definition["nodes"][0]))
    return definition


def _duplicate_output_key() -> dict:
    definition = two_node_definition()
    definition["nodes"][1]["save_as"] = "first"
    return definition


def _self_loop() -> dict:
    definition = two_node_definition()
    definition["edges"].append({"from": "alpha", "to": "alpha"})
    return definition


def _unknown_endpoint() -> dict:
    definition = two_node_definition()
    definition["edges"].append({"from": "alpha", "to": "ghost"})
    return definition


def _duplicate_edge_pair() -> dict:
    """Both condition branches to one node: the pair is duplicated."""
    definition = branch_definition()
    for edge in definition["edges"]:
        if edge["from"] == "route":
            edge["to"] = "left"
    return definition


def _second_entry() -> dict:
    definition = two_node_definition()
    definition["nodes"].append(transform("gamma", "1", save_as="third"))
    return definition


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (_duplicate_node_id, WORKFLOW_NODE_DUPLICATE_ID),
        (_duplicate_output_key, WORKFLOW_SAVE_AS_DUPLICATE),
        (_self_loop, WORKFLOW_EDGE_SELF_LOOP),
        (_unknown_endpoint, WORKFLOW_EDGE_UNKNOWN_NODE),
        (_duplicate_edge_pair, WORKFLOW_EDGE_DUPLICATE),
        (_second_entry, WORKFLOW_ENTRY_COUNT),
    ],
)
def test_invalid_graphs_fail_before_persistence(build, code) -> None:
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(build())
    assert caught.value.code == code


def test_cycles_and_multiple_roots_are_rejected() -> None:
    cyclic = branch_definition()
    cyclic["nodes"].append(transform("start", "1", save_as="start_out"))
    cyclic["edges"].extend(
        [
            {"from": "start", "to": "alpha"},
            {"from": "left", "to": "alpha"},
        ]
    )
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(cyclic)
    assert caught.value.code == WORKFLOW_CYCLE
    assert caught.value.details["cycle_nodes"]

    two_roots = two_node_definition()
    two_roots["nodes"].append(transform("gamma", "1", save_as="third"))
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(two_roots)
    assert caught.value.code == WORKFLOW_ENTRY_COUNT


def test_condition_node_requires_exactly_one_true_and_false_edge() -> None:
    definition = condition_definition()
    definition["edges"] = [edge for edge in definition["edges"] if edge.get("when") != "false"]

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition)
    assert caught.value.code == WORKFLOW_CONDITION_EDGES


def test_when_is_rejected_outside_condition_nodes() -> None:
    definition = condition_definition()
    definition["edges"].append({"from": "greet", "to": "farewell", "when": "true"})

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition)
    assert caught.value.code == "WORKFLOW_EDGE_UNEXPECTED_WHEN"


def test_references_must_be_declared_and_upstream() -> None:
    unknown_input = condition_definition()
    unknown_input["nodes"][3]["config"]["input"] = {"text": "{{ inputs.missing }}"}
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(unknown_input)
    assert caught.value.code == WORKFLOW_REFERENCE_UNKNOWN

    not_upstream = condition_definition()
    not_upstream["nodes"][0]["config"]["input"] = {"text": "{{ nodes.farewell.output }}"}
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(not_upstream)
    assert caught.value.code == WORKFLOW_REFERENCE_NOT_UPSTREAM

    unknown_save_as = condition_definition()
    unknown_save_as["nodes"][2]["config"]["expression"] = "outputs.ghost"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(unknown_save_as)
    assert caught.value.code == WORKFLOW_REFERENCE_UNKNOWN


def test_templates_are_restricted_and_path_safe() -> None:
    unterminated = condition_definition()
    unterminated["nodes"][2]["config"]["input"] = {"text": "{{ nodes.fetch.output"}
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(unterminated)
    assert caught.value.code == WORKFLOW_TEMPLATE_INVALID

    for bad_reference in ("nodes.fetch.output.deep", "../etc/passwd", "inputs", "nodes..output"):
        with pytest.raises(WorkflowCompileError) as caught:
            parse_reference(bad_reference)
        assert caught.value.code == "WORKFLOW_REFERENCE_INVALID"


def test_cel_expression_is_parsed_through_the_bounded_sandbox() -> None:
    definition = condition_definition()
    definition["nodes"][1]["config"]["expression"] = "outputs.fetched =="

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition)
    assert caught.value.code == "WORKFLOW_CEL_INVALID"


def test_approval_target_and_edges_are_mutually_exclusive() -> None:
    definition = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            transform("start", "1"),
            {
                "id": "approve",
                "type": "approval",
                "name": "Approve",
                "config": {
                    "approval_message": "ok?",
                    "approver_user_ids": [APPROVER],
                    "target_node_id": "done",
                },
            },
            transform("done", "1"),
        ],
        "edges": [{"from": "start", "to": "approve"}],
    }

    class Resolver:
        def check_tool(self, tool_name, parameters):
            return None

        def check_model(self, model):
            return None

        def check_knowledge_base(self, knowledge_base_id):
            return None

        def check_approver(self, user_id):
            return SemanticDecision.allowed()

    compiled = compile_workflow_definition(definition, resolver=Resolver())
    assert [edge.to_node_id for edge in compiled.node("approve").outgoing] == ["done"]
    assert compiled.node("done").upstream_node_ids == {"start", "approve"}
    assert compiled.semantic_checks == "passed"

    definition["edges"].append({"from": "approve", "to": "start"})
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == "WORKFLOW_APPROVAL_EDGES"


def test_semantic_resolution_fails_closed_and_surfaces_codes() -> None:
    definition = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "call",
                "type": "tool",
                "name": "Call tool",
                "config": {"tool_name": "not_installed", "parameters": {}},
            }
        ],
        "edges": [],
    }

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, require_semantic_resolution=True)
    assert caught.value.code == WORKFLOW_DEPENDENCY_UNAVAILABLE

    assert compile_workflow_definition(definition).semantic_checks == "skipped"

    class Resolver:
        def check_tool(self, tool_name, parameters):
            return SemanticDecision.refused(WORKFLOW_TOOL_UNAVAILABLE, "tool is not granted")

        def check_model(self, model):
            return SemanticDecision.refused(WORKFLOW_MODEL_NOT_CONFIGURED, "no model")

        def check_knowledge_base(self, knowledge_base_id):
            return SemanticDecision.allowed()

        def check_approver(self, user_id):
            return SemanticDecision.allowed()

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(
            definition, resolver=Resolver(), require_semantic_resolution=True
        )
    assert caught.value.code == WORKFLOW_TOOL_UNAVAILABLE
    assert caught.value.path == "nodes.call.config.tool_name"

    llm = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "ask",
                "type": "llm",
                "name": "Ask",
                "config": {"prompt": "hi"},
            }
        ],
        "edges": [],
    }
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(llm, resolver=Resolver(), require_semantic_resolution=True)
    assert caught.value.code == WORKFLOW_MODEL_NOT_CONFIGURED


def test_llm_node_knowledge_bases_follow_the_resolver_decision() -> None:
    """A refused base blocks the publish; an unresolvable answer does not."""

    visible = "00000000-0000-0000-0000-0000000000aa"
    hidden = "00000000-0000-0000-0000-0000000000bb"

    class Resolver:
        def check_tool(self, tool_name, parameters):
            return SemanticDecision.allowed()

        def check_model(self, model):
            return SemanticDecision.allowed()

        def check_knowledge_base(self, knowledge_base_id):
            if knowledge_base_id == hidden:
                return SemanticDecision.refused(
                    WORKFLOW_KNOWLEDGE_BASE_UNKNOWN, "base is not visible in this tenant"
                )
            return SemanticDecision.allowed()

        def check_approver(self, user_id):
            return SemanticDecision.allowed()

    definition = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "ask",
                "type": "llm",
                "name": "Ask",
                "config": {
                    "prompt": "hi",
                    "model": "tenant-model",
                    "knowledge_base_ids": [visible, hidden],
                },
            }
        ],
        "edges": [],
    }

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(
            definition, resolver=Resolver(), require_semantic_resolution=True
        )
    assert caught.value.code == WORKFLOW_KNOWLEDGE_BASE_UNKNOWN
    assert caught.value.path == "nodes.ask.config.knowledge_base_ids[1]"

    definition["nodes"][0]["config"]["knowledge_base_ids"] = [visible]
    assert (
        compile_workflow_definition(
            definition, resolver=Resolver(), require_semantic_resolution=True
        ).semantic_checks
        == "passed"
    )

    class Unresolvable(Resolver):
        # None is the resolver's "cannot answer here" answer, and the contract
        # tolerates it: only an explicit refusal blocks a publish.
        def check_knowledge_base(self, knowledge_base_id):
            return None

    assert (
        compile_workflow_definition(
            definition, resolver=Unresolvable(), require_semantic_resolution=True
        ).semantic_checks
        == "passed"
    )


def test_publish_requires_at_least_one_valid_approver() -> None:
    definition = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "approve",
                "type": "approval",
                "name": "Approve",
                "config": {"approval_message": "ok?", "approver_user_ids": [APPROVER]},
            }
        ],
        "edges": [],
    }

    class Resolver:
        def check_tool(self, tool_name, parameters):
            return None

        def check_model(self, model):
            return None

        def check_knowledge_base(self, knowledge_base_id):
            return None

        def check_approver(self, user_id):
            if user_id == APPROVER:
                return None
            return SemanticDecision.refused(WORKFLOW_APPROVER_INVALID, "not a tenant member")

    with pytest.raises(WorkflowCompileError) as caught:
        # Every declared approver has left the tenant.
        definition["nodes"][0]["config"]["approver_user_ids"] = [
            "9d1e5b3c-7a42-4f18-8c62-5b0d9e3a7f21"
        ]
        compile_workflow_definition(definition, resolver=Resolver())
    # Nobody can approve, so the version must not reach a tenant.
    assert caught.value.code == "APPROVAL_NO_VALID_APPROVER"
    assert caught.value.path == "nodes.approve.config.approver_user_ids"

    # A departed colleague is tolerated as long as someone else can still decide.
    definition["nodes"][0]["config"]["approver_user_ids"] = [
        APPROVER,
        "8b7d4c2a-19f5-4a33-9c5e-2f7b6d0a1e44",
    ]
    compiled = compile_workflow_definition(definition, resolver=Resolver())
    assert compiled.definition_sha256


def test_stored_definition_hash_round_trip() -> None:
    compiled = compile_workflow_definition(hello_definition())
    stored = json.loads(canonical_definition_json(compiled.definition))

    reloaded = compile_stored_definition(stored, compiled.definition_sha256)
    assert reloaded.definition_sha256 == compiled.definition_sha256

    tampered = dict(stored)
    tampered["inputs"] = {}
    with pytest.raises(WorkflowCompileError) as caught:
        compile_stored_definition(tampered, compiled.definition_sha256)
    assert caught.value.code == WORKFLOW_VERSION_HASH_MISMATCH

    with pytest.raises(WorkflowCompileError):
        verify_definition_hash(stored, "0" * 64)
