"""The explicit ``input`` / ``knowledge`` / ``output`` nodes execute (A-07).

Compiling a definition is half the promise: a definition the compiler accepts
must also run.  These tests pin what each new node does at run time, and — for
the one that needs a deployment capability — that a missing retriever fails the
step instead of quietly returning "no passages found".
"""

from __future__ import annotations

from typing import Any

from octop.infra.workbuddy.runtime import graph_from_compiled, run_graph
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
