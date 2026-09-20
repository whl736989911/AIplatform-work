"""The explicit ``input`` / ``knowledge`` / ``output`` nodes execute (A-07).

Compiling a definition is half the promise: a definition the compiler accepts
must also run.  These tests pin what each new node does at run time, and — for
the one that needs a deployment capability — that a missing retriever fails the
step instead of quietly returning "no passages found".
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.infra.errors import OctopError
from octop.infra.workbuddy.runtime import (
    graph_from_compiled,
    run_graph,
    validate_answers,
)
from octop.infra.workbuddy.workflow_compiler import (
    SemanticDecision,
    compile_workflow_definition,
)

KB = "00000000-0000-0000-0000-0000000000aa"


class Resolver:
    def check_tool(self, tool_name, parameters):
        return SemanticDecision.allowed()

    def check_model(self, model):
        return SemanticDecision.allowed()

    def check_knowledge_base(self, knowledge_base_id):
        return SemanticDecision.allowed()

    def check_approver(self, user_id):
        return SemanticDecision.allowed()


def _graph(definition: dict[str, Any]) -> Any:
    return graph_from_compiled(
        compile_workflow_definition(definition, resolver=Resolver()), version_id="v1"
    )


def _pipeline(*, with_output: bool = True) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
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
            "config": {"knowledge_base_ids": [KB], "query": "{{ nodes.ask.output }}"},
            "save_as": "passages",
        },
    ]
    edges = [{"from": "ask", "to": "lookup"}]
    if with_output:
        nodes.append(
            {
                "id": "answer",
                "type": "output",
                "name": "Publish the answer",
                "config": {"value": "{{ nodes.lookup.output }}"},
            }
        )
        edges.append({"from": "lookup", "to": "answer"})
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"question": {"type": "string", "required": True}},
        "nodes": nodes,
        "edges": edges,
    }


def test_an_input_node_hands_a_declared_input_to_the_graph() -> None:
    definition = _pipeline()
    definition["nodes"] = definition["nodes"][:1]  # just the input node
    definition["edges"] = []
    run = run_graph(_graph(definition), inputs={"question": "how do I file an expense"})
    assert run.status == "success", run
    assert run.steps[0].output == "how do I file an expense"


def test_an_input_node_that_the_run_did_not_supply_fails_its_step() -> None:
    definition = _pipeline()
    definition["nodes"] = definition["nodes"][:1]
    definition["edges"] = []
    run = run_graph(_graph(definition), inputs={})
    assert run.status == "failed", run
    assert run.steps[0].error_code == "WORKBUDDY_VALIDATION_FAILED"


def test_a_knowledge_node_uses_the_injected_retriever() -> None:
    seen: list[Any] = []

    def retrieve(node, activation):
        seen.append((node.id, activation))
        return [{"document_id": "doc-1", "text": "expenses are filed in the portal"}]

    run = run_graph(
        _graph(_pipeline(with_output=False)),
        inputs={"question": "expenses?"},
        retrieve_knowledge=retrieve,
    )
    assert run.status == "success", run
    assert [node_id for node_id, _ in seen] == ["lookup"]
    # The retriever is handed the activation, so it sees the rendered query.
    assert seen[0][1]["inputs"] == {"question": "expenses?"}
    assert run.steps[-1].output[0]["document_id"] == "doc-1"


def test_a_knowledge_node_without_a_retriever_fails_closed() -> None:
    run = run_graph(_graph(_pipeline(with_output=False)), inputs={"question": "expenses?"})
    assert run.status == "failed", run
    failing = [step for step in run.steps if step.status == "failed"]
    assert failing and failing[0].error_code == "WORKBUDDY_DEPENDENCY_UNAVAILABLE"


def test_an_output_node_renders_the_run_result() -> None:
    def retrieve(node, activation):
        return ["the answer"]

    run = run_graph(
        _graph(_pipeline()),
        inputs={"question": "expenses?"},
        retrieve_knowledge=retrieve,
    )
    assert run.status == "success", run
    last = run.steps[-1]
    assert last.node_id == "answer"
    # ``{{ nodes.lookup.output }}`` is the whole retrieved list, not a scalar.
    assert last.output == ["the answer"]


# --------------------------------------------------------------------------- #
# ask: a run that stops to ask a person (A-08)
# --------------------------------------------------------------------------- #

MEMBER = "11111111-1111-1111-1111-111111111111"


def _ask_form() -> list[dict[str, Any]]:
    return [
        {"name": "account", "label": "Account", "type": "select", "options": ["A", "B"]},
        {"name": "amount", "label": "Amount", "type": "number"},
        {"name": "note", "label": "Note", "type": "text", "required": False},
    ]


def _ask(*, with_downstream: bool = False) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {
            "id": "collect",
            "type": "ask",
            "name": "Ask the requester",
            "config": {
                "prompt": "Which account should this post to?",
                "assignee_user_ids": [MEMBER],
                "fields": _ask_form(),
            },
            "save_as": "answer",
        }
    ]
    edges: list[dict[str, Any]] = []
    if with_downstream:
        nodes.append(
            {
                "id": "post",
                "type": "output",
                "name": "Publish what was collected",
                "config": {"value": "{{ nodes.collect.output }}"},
            }
        )
        edges.append({"from": "collect", "to": "post"})
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {},
        "nodes": nodes,
        "edges": edges,
    }


def test_an_ask_node_parks_the_run_for_an_answer() -> None:
    run = run_graph(_graph(_ask()), inputs={}, resolve_assignees=lambda node: [(7, None)])
    assert run.status == "waiting_input", run
    assert run.waiting_input_node_id == "collect"
    # The assignees travel with the park so the caller can open the question for
    # exactly the people who may answer it.
    assert run.input_assignees["collect"] == ((7, None),)
    assert run.steps[-1].status == "waiting_input"


def test_an_ask_node_without_an_eligible_assignee_fails_in_place() -> None:
    run = run_graph(_graph(_ask()), inputs={}, resolve_assignees=lambda node: [])
    # Nobody can answer, so parking would strand the run: the node fails where it
    # stands instead of waiting forever.
    assert run.status == "failed", run
    failing = [step for step in run.steps if step.status == "failed"]
    assert failing and failing[0].error_code == "ASK_NO_VALID_ASSIGNEE"


def test_an_ask_node_without_a_resolver_still_parks() -> None:
    # A deployment that wired no membership resolver cannot prove nobody is
    # eligible, so the run parks rather than inventing a failure.
    run = run_graph(_graph(_ask()), inputs={})
    assert run.status == "waiting_input", run
    assert run.input_assignees == {}


def test_a_recorded_answer_settles_the_node_and_flows_downstream() -> None:
    def never_called(node: Any) -> list[tuple[int, str | None]]:
        raise AssertionError("an answered ask node must not resolve assignees")

    run = run_graph(
        _graph(_ask(with_downstream=True)),
        inputs={},
        answers={"collect": {"account": "A", "amount": 1200}},
        resolve_assignees=never_called,
    )
    assert run.status == "success", run
    collected = run.steps[0]
    assert collected.status == "success"
    assert collected.output == {"account": "A", "amount": 1200}
    # The answer is the node's output, so ``{{ nodes.collect.output }}`` is what
    # the rest of the graph reads: the question's fields become the run's data.
    assert run.steps[-1].output == {"account": "A", "amount": 1200}


def test_the_answer_validator_keeps_the_declared_values() -> None:
    cleaned = validate_answers({"fields": _ask_form()}, {"account": "B", "amount": 1200})
    # ``note`` is optional and therefore absent, not stored as null.
    assert cleaned == {"account": "B", "amount": 1200}


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"account": "A"}, "is required"),
        ({"account": "C", "amount": 1}, "must be one of"),
        ({"account": "A", "amount": "many"}, "must be a number"),
        ({"account": "A", "amount": True}, "must be a number"),
        ({"account": "A", "amount": 1, "extra": 2}, "does not declare"),
    ],
)
def test_the_answer_validator_refuses_what_the_form_does_not_allow(
    values: dict[str, Any], reason: str
) -> None:
    with pytest.raises(OctopError) as excinfo:
        validate_answers({"fields": _ask_form()}, values)
    assert reason in excinfo.value.message, excinfo.value.message


def test_the_answer_validator_refuses_a_field_type_the_schema_does_not_define() -> None:
    form = {"fields": [{"name": "odd", "label": "Odd", "type": "quantum"}]}
    with pytest.raises(OctopError) as excinfo:
        validate_answers(form, {"odd": "anything"})
    assert "unsupported type" in excinfo.value.message
