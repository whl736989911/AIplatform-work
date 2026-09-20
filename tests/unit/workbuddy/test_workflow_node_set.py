"""The explicit node set: ``input`` / ``knowledge`` / ``output`` (A-07).

Before this slice the three were implicit: a run's inputs arrived through
``{{ inputs.<name> }}`` templates, retrieval happened inside an ``llm`` node's
``knowledge_base_ids``, and "what the run produced" was whatever the exit nodes
happened to leave behind.  These tests pin the explicit forms *and* the rule that
made them safe to add: definitions written before them keep compiling, and the
graph rules they change (entries, terminals) change only for definitions that
actually use them.
"""

from __future__ import annotations

import pytest

from octop.infra.workbuddy.workflow_compiler import (
    WORKFLOW_ASK_FIELDS_DUPLICATE,
    WORKFLOW_ENTRY_COUNT,
    WORKFLOW_INPUT_NODE_UNKNOWN,
    WORKFLOW_KNOWLEDGE_BASE_UNKNOWN,
    WORKFLOW_OUTPUT_DUPLICATE,
    WORKFLOW_OUTPUT_NOT_TERMINAL,
    WORKFLOW_SCHEMA_INVALID,
    SemanticDecision,
    WorkflowCompileError,
    compile_workflow_definition,
)

KB = "00000000-0000-0000-0000-0000000000aa"


def manual_trigger() -> dict:
    return {"type": "manual", "config": {}}


class Resolver:
    """Allows everything; models a tenant where the knowledge base is reachable."""

    def check_tool(self, tool_name, parameters):
        return SemanticDecision.allowed()

    def check_model(self, model):
        return SemanticDecision.allowed()

    def check_knowledge_base(self, knowledge_base_id):
        return SemanticDecision.allowed()

    def check_approver(self, user_id):
        return SemanticDecision.allowed()


def pipeline() -> dict:
    """input → knowledge → output, the shape the wizard's four steps build."""
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "inputs": {"question": {"type": "string", "required": True}},
        "nodes": [
            {
                "id": "ask",
                "type": "input",
                "name": "Read the question",
                "config": {"input": "question"},
                "save_as": "asked",
            },
            {
                "id": "lookup",
                "type": "knowledge",
                "name": "Search the handbook",
                "config": {
                    "knowledge_base_ids": [KB],
                    "query": "{{ nodes.ask.output }}",
                    "top_k": 5,
                },
                "save_as": "passages",
            },
            {
                "id": "answer",
                "type": "output",
                "name": "Publish the answer",
                "config": {"value": "{{ nodes.lookup.output }}"},
            },
        ],
        "edges": [
            {"from": "ask", "to": "lookup"},
            {"from": "lookup", "to": "answer"},
        ],
    }


def test_an_explicit_input_knowledge_output_pipeline_compiles() -> None:
    compiled = compile_workflow_definition(pipeline(), resolver=Resolver())
    # The run starts by reading the declared input; that node *is* the entry.
    assert compiled.entry_node_id == "ask"
    assert compiled.exit_node_ids == ("answer",)
    assert [(node.node_id, node.node_type) for node in compiled.nodes] == [
        ("answer", "output"),
        ("ask", "input"),
        ("lookup", "knowledge"),
    ]


def test_an_input_node_must_name_a_declared_input() -> None:
    definition = pipeline()
    definition["nodes"][0]["config"]["input"] = "never_declared"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == WORKFLOW_INPUT_NODE_UNKNOWN
    assert caught.value.path == "nodes.ask.config.input"


def test_several_input_roots_are_allowed() -> None:
    """Two inputs may feed one pipeline; that is not "two entry nodes"."""
    definition = pipeline()
    definition["inputs"]["locale"] = {"type": "string", "required": False}
    definition["nodes"].append(
        {
            "id": "locale",
            "type": "input",
            "name": "Read the locale",
            "config": {"input": "locale"},
            "save_as": "loc",
        }
    )
    definition["edges"].append({"from": "locale", "to": "lookup"})
    compiled = compile_workflow_definition(definition, resolver=Resolver())
    # Both inputs are sources; the run needs no "extra" entry to reach them.
    assert compiled.entry_node_id == "ask"
    assert compiled.exit_node_ids == ("answer",)


def test_two_real_entries_are_still_refused() -> None:
    """A definition without input nodes keeps the original single-entry rule."""
    definition = {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "nodes": [
            {
                "id": "first",
                "type": "transform",
                "name": "One root",
                "config": {"input": {}, "expression": "'a'"},
            },
            {
                "id": "second",
                "type": "transform",
                "name": "Another root",
                "config": {"input": {}, "expression": "'b'"},
            },
        ],
        "edges": [],
    }
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == WORKFLOW_ENTRY_COUNT


def test_at_most_one_output_node() -> None:
    definition = pipeline()
    definition["nodes"].append(
        {
            "id": "second_output",
            "type": "output",
            "name": "Another result",
            "config": {"value": "'x'"},
        }
    )
    definition["edges"].append({"from": "ask", "to": "second_output"})
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == WORKFLOW_OUTPUT_DUPLICATE


def test_an_output_node_must_be_terminal() -> None:
    definition = pipeline()
    definition["nodes"].append(
        {
            "id": "after",
            "type": "transform",
            "name": "Runs after the result",
            "config": {"input": {}, "expression": "'x'"},
        }
    )
    definition["edges"].append({"from": "answer", "to": "after"})
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == WORKFLOW_OUTPUT_NOT_TERMINAL
    assert caught.value.path == "nodes.answer.config.value"


def test_a_knowledge_node_requires_a_reachable_base() -> None:
    class Blind(Resolver):
        def check_knowledge_base(self, knowledge_base_id):
            return SemanticDecision.refused(
                WORKFLOW_KNOWLEDGE_BASE_UNKNOWN, "base is not visible in this tenant"
            )

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(pipeline(), resolver=Blind())
    assert caught.value.code == WORKFLOW_KNOWLEDGE_BASE_UNKNOWN
    assert caught.value.path == "nodes.lookup.config.knowledge_base_ids[0]"


def test_the_template_fields_of_the_new_nodes_are_checked() -> None:
    """``knowledge.query`` and ``output.value`` accept templates, and both are validated."""
    definition = pipeline()
    definition["nodes"][2]["config"]["value"] = "{{ nodes.nowhere.output }}"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == "WORKFLOW_REFERENCE_UNKNOWN"
    assert caught.value.path == "nodes.answer.config.value"


# --------------------------------------------------------------------------- #
# ask: a node that stops the run to ask a person (A-08)
# --------------------------------------------------------------------------- #

MEMBER = "11111111-1111-1111-1111-111111111111"
OTHER_MEMBER = "22222222-2222-2222-2222-222222222222"


def ask_definition(*, fields: list[dict] | None = None, assignees: list[str] | None = None) -> dict:
    """One ``ask`` node, which is entry and terminal at once."""
    return {
        "schema_version": 1,
        "trigger": manual_trigger(),
        "inputs": {},
        "nodes": [
            {
                "id": "collect",
                "type": "ask",
                "name": "Ask the requester",
                "config": {
                    "prompt": "Which account should this post to?",
                    "assignee_user_ids": assignees if assignees is not None else [MEMBER],
                    "fields": fields
                    if fields is not None
                    else [
                        {
                            "name": "account",
                            "label": "Account",
                            "type": "select",
                            "options": ["A", "B"],
                        }
                    ],
                },
            }
        ],
        "edges": [],
    }


def test_an_ask_node_compiles_with_its_form() -> None:
    compiled = compile_workflow_definition(ask_definition(), resolver=Resolver())
    assert compiled.node_by_id["collect"].node_type == "ask"


def test_an_ask_node_refuses_two_fields_with_the_same_name() -> None:
    definition = ask_definition(
        fields=[
            {"name": "amount", "label": "Amount", "type": "number"},
            {"name": "amount", "label": "Amount again", "type": "number"},
        ]
    )
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    # Field names are the keys of the submitted mapping, so a duplicate would let
    # the later field silently win.
    assert caught.value.code == WORKFLOW_ASK_FIELDS_DUPLICATE
    assert caught.value.path == "nodes.collect.config.fields[1].name"


def test_an_ask_node_refuses_a_select_without_options() -> None:
    definition = ask_definition(fields=[{"name": "account", "label": "Account", "type": "select"}])
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    assert caught.value.code == WORKFLOW_SCHEMA_INVALID


def test_an_ask_node_cannot_declare_an_approval_target() -> None:
    definition = ask_definition()
    definition["nodes"][0]["config"]["target_node_id"] = "collect"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(definition, resolver=Resolver())
    # An ask node's config is closed: targeting another node's output is an
    # authority an approval carries and a question does not.
    assert caught.value.code == WORKFLOW_SCHEMA_INVALID


def test_an_ask_node_needs_one_assignee_the_tenant_can_still_see() -> None:
    class Nobody:
        """Every declared assignee has left the tenant."""

        def check_tool(self, tool_name, parameters):
            return SemanticDecision.allowed()

        def check_model(self, model):
            return SemanticDecision.allowed()

        def check_knowledge_base(self, knowledge_base_id):
            return SemanticDecision.allowed()

        def check_approver(self, user_id):
            return SemanticDecision.refused("MEMBER_GONE", "member left the tenant")

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_definition(ask_definition(), resolver=Nobody())
    assert caught.value.code == "ASK_NO_VALID_ASSIGNEE"
    assert caught.value.path == "nodes.collect.config.assignee_user_ids"

    class SecondOnly:
        def check_tool(self, tool_name, parameters):
            return SemanticDecision.allowed()

        def check_model(self, model):
            return SemanticDecision.allowed()

        def check_knowledge_base(self, knowledge_base_id):
            return SemanticDecision.allowed()

        def check_approver(self, user_id):
            if user_id == OTHER_MEMBER:
                return SemanticDecision.allowed()
            return SemanticDecision.refused("MEMBER_GONE", "member left the tenant")

    # One reachable assignee is enough to publish, exactly as one valid approver
    # is: a departure must not make an otherwise sound workflow unpublishable.
    compile_workflow_definition(
        ask_definition(assignees=[MEMBER, OTHER_MEMBER]), resolver=SecondOnly()
    )
