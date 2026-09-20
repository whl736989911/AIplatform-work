"""A model may describe a workflow; it may not write one (A-15).

The contract is that the runtime only executes definitions the compiler accepted, so
these tests are about where the restriction actually lives: the authoring document
is checked against the schema-derived vocabulary, widened into a definition by a
deterministic lowering, and then judged by the same compiler every other definition
faces.  What is being protected is that a model's output cannot become a graph the
compiler never saw, an unknown node kind, a field the schema does not declare, or a
reference to a step that does not exist yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from octop.infra.workbuddy.authoring import (
    AuthoringDiagnostic,
    author_workflow,
    lower_authoring,
    validate_authoring,
)
from octop.infra.workbuddy.workflow_compiler import compile_workflow_definition


def _document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "问候流水线",
        "trigger": {"type": "manual"},
        "inputs": [{"key": "who", "type": "string", "required": True}],
        "steps": [
            {
                "id": "greet",
                "kind": "transform",
                "purpose": "把输入包成一句问候",
                "config": {"input": {"who": "{{ inputs.who }}"}, "expression": "input"},
            },
            {
                "id": "polish",
                "kind": "llm",
                "purpose": "把问候润色得更自然",
                "uses": ["greet"],
                "config": {"prompt": "润色这段问候：{{ steps.greet.output }}"},
            },
        ],
    }
    base.update(overrides)
    return base


@dataclass
class ScriptedSource:
    """A stand-in author: the first document, then one repair per revision."""

    documents: list[Mapping[str, Any]]
    asked: list[tuple[Mapping[str, Any], tuple[AuthoringDiagnostic, ...]]] = field(
        default_factory=list
    )

    def draft(
        self, *, request: str, metadata: Mapping[str, Any], existing: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        return self.documents[0]

    def revise(
        self,
        *,
        document: Mapping[str, Any],
        diagnostics: Sequence[AuthoringDiagnostic],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.asked.append((document, tuple(diagnostics)))
        return self.documents[len(self.asked)]


def test_a_description_becomes_a_definition_that_compiles() -> None:
    source = ScriptedSource([_document()])
    outcome = author_workflow(request="给新同事发一句问候", source=source)
    assert outcome.ok, outcome.to_payload()
    assert outcome.rounds == 1, outcome.rounds
    assert outcome.diagnostics == (), outcome.diagnostics
    definition = outcome.definition
    assert definition is not None
    # The compiler is the judge, not this test: it accepts what the lowering produced.
    compile_workflow_definition(definition)
    assert [node["id"] for node in definition["nodes"]] == ["greet", "polish"], definition
    assert [node["save_as"] for node in definition["nodes"]] == ["greet", "polish"], definition
    assert definition["edges"] == [{"from": "greet", "to": "polish"}], definition
    # The author writes ``steps``; the runtime reads ``nodes``. One translation.
    prompt = definition["nodes"][1]["config"]["prompt"]
    assert "{{ nodes.greet.output }}" in prompt and "{{ steps.greet" not in prompt, prompt
    # And the person gets the model's own words about what each step is for.
    payload = outcome.to_payload()
    assert payload["steps"][1]["purpose"] == "把问候润色得更自然", payload
    assert payload["steps"][1]["uses"] == ["greet"], payload


def test_the_compiler_refusal_is_handed_back_and_one_repair_is_enough() -> None:
    bad = _document(trigger={"type": "cron", "config": {}})
    good = _document(
        trigger={
            "type": "cron",
            "config": {"cron_expression": "0 9 * * *", "timezone": "Asia/Shanghai"},
        }
    )
    source = ScriptedSource([bad, good])
    outcome = author_workflow(request="每天早上九点发问候", source=source)
    assert outcome.ok, outcome.to_payload()
    assert outcome.rounds == 2, outcome.rounds
    assert len(source.asked) == 1, source.asked
    # What went back was the *compiler's* complaint about the trigger, not ours.
    handed_back = [item.code for item in source.asked[0][1]]
    assert handed_back and all(code.startswith("WORKFLOW_") for code in handed_back), handed_back
    assert outcome.diagnostics == (), outcome.to_payload()


def test_a_document_that_never_compiles_is_reported_rather_than_stored() -> None:
    bad = _document(trigger={"type": "cron", "config": {}})
    source = ScriptedSource([bad, bad])
    outcome = author_workflow(request="无论如何都写不对", source=source)
    assert outcome.ok is False, outcome.to_payload()
    assert outcome.definition is None, outcome.to_payload()
    assert outcome.rounds == 2, outcome.rounds
    assert outcome.diagnostics, outcome.to_payload()


def test_a_forward_reference_is_refused_before_the_compiler_sees_anything() -> None:
    document = _document(
        steps=[
            {
                "id": "polish",
                "kind": "llm",
                "purpose": "先用后一步的产出",
                "uses": ["greet"],
                "config": {"prompt": "{{ steps.greet.output }}"},
            },
            {
                "id": "greet",
                "kind": "transform",
                "purpose": "之后才产出",
                "config": {"input": {"who": "{{ inputs.who }}"}, "expression": "input"},
            },
        ]
    )
    problems = validate_authoring(document)
    # Two facets of the same mistake: the order is wrong, and the reference it
    # makes is therefore to a step that does not exist yet.
    assert {item.code for item in problems} == {"AUTHORING_STEP_ORDER", "AUTHORING_REFERENCE"}, (
        problems
    )
    # A cycle is not refused as a cycle: it is unrepresentable, because ``uses`` may
    # only name earlier steps and the document's order is the topological order.
    source = ScriptedSource([document, document])
    outcome = author_workflow(request="环形", source=source)
    assert outcome.ok is False, outcome.to_payload()


def test_an_undeclared_config_field_is_refused() -> None:
    document = _document(
        steps=[
            {
                "id": "greet",
                "kind": "transform",
                "purpose": "字段是编的",
                "config": {"input": {"who": "x"}, "expression": "input", "temperature": 0.7},
            }
        ]
    )
    problems = validate_authoring(document)
    assert [item.code for item in problems] == ["AUTHORING_CONFIG_FIELD"], problems
    assert "temperature" in problems[0].message, problems[0]


def test_a_missing_required_config_field_is_refused() -> None:
    document = _document(
        steps=[{"id": "greet", "kind": "llm", "purpose": "没有提示词", "config": {}}]
    )
    problems = validate_authoring(document)
    assert [item.code for item in problems] == ["AUTHORING_CONFIG_REQUIRED"], problems


def test_an_unknown_node_kind_is_refused() -> None:
    document = _document(
        steps=[{"id": "greet", "kind": "http_request", "purpose": "不存在的类型", "config": {}}]
    )
    problems = validate_authoring(document)
    assert [item.code for item in problems] == ["AUTHORING_NODE_KIND"], problems


def test_duplicate_step_ids_are_refused() -> None:
    step = {
        "id": "greet",
        "kind": "transform",
        "purpose": "重复",
        "config": {"input": {"who": "x"}, "expression": "input"},
    }
    problems = validate_authoring(_document(steps=[step, dict(step)]))
    assert [item.code for item in problems] == ["AUTHORING_STEP_ID"], problems


def test_lowering_is_total_and_translates_references_everywhere() -> None:
    document = _document(
        steps=[
            {
                "id": "fetch",
                "kind": "transform",
                "purpose": "取数",
                "config": {"input": {"who": "{{ inputs.who }}"}, "expression": "input"},
            },
            {
                "id": "ask",
                "kind": "ask",
                "purpose": "问一个只有人知道的事实",
                "uses": ["fetch"],
                "config": {
                    "prompt": "关于 {{ steps.fetch.output }} 的发票号是？",
                    "assignee_user_ids": [],
                    "fields": [{"name": "invoice", "label": "发票号", "type": "string"}],
                },
            },
        ]
    )
    lowered = lower_authoring(document)
    ask = lowered["nodes"][1]
    # Nested values are translated too, not just top-level strings.
    assert "{{ nodes.fetch.output }}" in ask["config"]["prompt"], ask
    assert "{{ steps." not in str(lowered), lowered
