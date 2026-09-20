"""The per-node facts of an execution detail (contract §4.6).

A run has to be debuggable node by node: what each node was given, what it
produced, how long it took and what it cost. The drawer renders whatever the
server returns, so a fact that is missing from the payload is a fact an operator
cannot see; these tests pin the shape without a database, and pin that a value
this deployment never recorded is ``None`` rather than absent.

The write path is covered where it runs: the engine test proves that a step
carries the activation it was dispatched with (the same object the adapter was
handed) plus the usage the adapter reported, and that a node which called no
model records no usage at all.
"""

from __future__ import annotations

from typing import Any

from octop.infra.workbuddy.runtime import (
    ExecutionView,
    StepOutcome,
    StepRunView,
    graph_from_compiled,
    run_graph,
)
from octop.infra.workbuddy.workflow_compiler import compile_workflow_definition

# Every fact the execution detail promises for one step. A key missing here is a
# key the frontend has to guess about.
STEP_KEYS = (
    "node_id",
    "node_type",
    "attempt",
    "status",
    "save_as",
    "skip_reason",
    "started_at",
    "finished_at",
    "duration_ms",
    "input",
    "input_sha256",
    "output",
    "token_usage",
    "error_code",
    "tool_id",
    "tool_call_key",
    "dispatch_intent_at",
)


def _step(**overrides: Any) -> StepRunView:
    fields: dict[str, Any] = {
        "node_id": "review",
        "node_type": "approval",
        "attempt": 1,
        "status": "waiting_approval",
        "save_as": None,
        "error_code": None,
    }
    fields.update(overrides)
    return StepRunView(**fields)


def test_a_step_returns_every_fact_it_never_recorded_as_none() -> None:
    """A step that dispatched nothing still answers with the whole contract."""
    payload = _step().to_payload()

    assert set(payload) == set(STEP_KEYS)
    assert payload["input"] is None
    assert payload["input_sha256"] is None
    assert payload["output"] is None
    assert payload["token_usage"] is None
    assert payload["duration_ms"] is None
    assert payload["started_at"] is None
    assert payload["finished_at"] is None
    assert payload["skip_reason"] is None
    assert payload["attempt"] == 1


def test_a_step_returns_the_input_output_and_usage_it_recorded() -> None:
    """The recorded facts survive the projection unchanged, whatever their shape."""
    activation = {
        "inputs": {"who": "unit"},
        "outputs": {"greeting": {"greeting": "hello unit"}},
        "input": {"greeting": "hello unit"},
        "node": {"id": "hello", "name": "Build greeting", "type": "transform"},
        "workflow": {"version_id": "v1", "definition_sha256": "sha"},
    }
    payload = _step(
        node_id="hello",
        node_type="transform",
        status="success",
        input=activation,
        input_sha256="a" * 64,
        # A node's output is whatever the node produced, not necessarily an object.
        output="hello unit",
        token_usage={"total_tokens": 42, "input_tokens": 30},
        duration_ms=7,
    ).to_payload()

    assert payload["input"] == activation
    assert payload["input_sha256"] == "a" * 64
    assert payload["output"] == "hello unit"
    assert payload["token_usage"] == {"total_tokens": 42, "input_tokens": 30}
    assert payload["duration_ms"] == 7


def test_execution_detail_returns_the_execution_facts() -> None:
    payload = ExecutionView(
        id="9f1d5a2e-0000-4000-8000-000000000001",
        workflow_id="9f1d5a2e-0000-4000-8000-000000000002",
        workflow_version_id="9f1d5a2e-0000-4000-8000-000000000003",
        status="success",
        trigger_type="api",
        inputs={"who": "unit"},
        outputs={"greeting": {"greeting": "hello unit"}},
        error_code=None,
        error_message=None,
        created_by_user_id=7,
        created_at="2026-09-20T00:00:00+00:00",
        started_at="2026-09-20T00:00:00+00:00",
        finished_at="2026-09-20T00:00:01+00:00",
        active_duration_ms=1000,
        token_usage=42,
    ).to_payload()

    assert payload["inputs"] == {"who": "unit"}
    assert payload["outputs"] == {"greeting": {"greeting": "hello unit"}}
    assert payload["active_duration_ms"] == 1000
    assert payload["token_usage"] == 42
    assert payload["error_code"] is None


class _RecordingPort:
    """A model adapter that answers with usage and remembers what it was given."""

    def __init__(self) -> None:
        self.activations: list[dict[str, Any]] = []

    def execute_llm(self, *, node: Any, activation: Any) -> Any:
        self.activations.append(dict(activation))
        return {"text": "hello", "usage": {"total_tokens": 42, "input_tokens": 30}}

    def execute_tool(
        self, *, node: Any, activation: Any, idempotency_key: str
    ) -> Any:  # pragma: no cover - no tool node in these definitions
        raise AssertionError("this workflow has no tool node")

    def respond_chat(
        self, *, session_id: str, message: str, history: Any
    ) -> Any:  # pragma: no cover - chat is not part of a workflow run
        raise AssertionError("chat is not part of this workflow")


def _graph(definition: dict[str, Any]) -> Any:
    return graph_from_compiled(compile_workflow_definition(definition), version_id="v1")


def test_a_model_step_records_the_input_it_was_dispatched_with_and_its_usage() -> None:
    definition = {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "summarise",
                "type": "llm",
                "name": "Summarise",
                "config": {"model": "test-model", "prompt": "summarise {{ inputs.who }}"},
                "save_as": "summary",
            }
        ],
        "edges": [],
    }
    port = _RecordingPort()

    run = run_graph(_graph(definition), inputs={"who": "unit"}, effects=port)

    assert run.status == "success", run
    step = run.steps[0]
    assert isinstance(step, StepOutcome)
    # The recorded input is the activation the adapter was handed, not a re-render.
    assert step.input == port.activations[0]
    assert step.input is not None
    assert step.input["inputs"] == {"who": "unit"}
    assert step.token_usage == {"total_tokens": 42, "input_tokens": 30}
    # The usage object decides the execution's token total, so it must not have
    # changed shape on the way in.
    assert run.tokens == 42, run.tokens


def test_a_node_that_calls_no_model_records_its_input_and_no_usage() -> None:
    definition = {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {
                    "input": {"greeting": "hello {{ inputs.who }}"},
                    "expression": "input",
                },
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }

    run = run_graph(_graph(definition), inputs={"who": "unit"})

    assert run.status == "success", run
    step = run.steps[0]
    assert step.input is not None
    # The evaluation context carries the rendered input the expression resolved.
    assert step.input["input"] == {"greeting": "hello unit"}
    assert step.input["inputs"] == {"who": "unit"}
    assert step.token_usage is None
    assert step.output == {"greeting": "hello unit"}
