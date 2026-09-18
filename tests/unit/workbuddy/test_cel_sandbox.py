from __future__ import annotations

import time
from dataclasses import replace

import pytest

from octop.infra.workbuddy import CELSandboxError, CELSandboxLimits, evaluate_cel


def test_has_and_lazy_ternary_preserve_json_types() -> None:
    result = evaluate_cel(
        "has(outputs.accepted) ? 1 / 0 : inputs",
        {
            "inputs": {"count": 7, "enabled": True, "labels": ["a", "b"]},
            "outputs": {},
        },
    )

    assert result.value == {"count": 7, "enabled": True, "labels": ["a", "b"]}
    assert result.value_type == "MapType"
    assert result.stats.runner == "InterpretedRunner"
    assert 0 < result.stats.evaluated_nodes <= CELSandboxLimits().evaluation_nodes


def test_host_function_is_not_available() -> None:
    with pytest.raises(CELSandboxError) as caught:
        evaluate_cel('__import__("os")', {})

    assert caught.value.code == "CEL_EVALUATION_ERROR"


def test_expression_length_is_rejected_before_worker_start() -> None:
    limits = replace(CELSandboxLimits(), expression_length=8)

    with pytest.raises(CELSandboxError) as caught:
        evaluate_cel("true || false", {}, limits=limits)

    assert caught.value.code == "CEL_EXPRESSION_TOO_LONG"


def test_runtime_node_budget_counts_macro_iterations() -> None:
    limits = replace(CELSandboxLimits(), evaluation_nodes=40)

    with pytest.raises(CELSandboxError) as caught:
        evaluate_cel("[1, 2, 3, 4, 5, 6].map(x, x + 1)", {}, limits=limits)

    assert caught.value.code == "CEL_COST_EXCEEDED"


def test_deep_ast_is_rejected() -> None:
    limits = replace(CELSandboxLimits(), ast_depth=12)

    with pytest.raises(CELSandboxError) as caught:
        evaluate_cel("1 + (2 + (3 + (4 + (5 + 6))))", {}, limits=limits)

    assert caught.value.code == "CEL_AST_DEPTH_EXCEEDED"


def test_library_startup_is_not_charged_to_the_evaluation_budget() -> None:
    """A cold worker pays for the CEL import out of its *startup* budget.

    The import costs more than the default evaluation budget on a loaded
    machine, so charging it there reported correct evaluations as CEL_TIMEOUT.
    """
    limits = replace(CELSandboxLimits(), evaluation_timeout_seconds=0.5)
    started = time.perf_counter()

    result = evaluate_cel("1 + 1", {}, limits=limits)

    assert result.value == 2
    assert result.stats.duration_ms < limits.evaluation_timeout_seconds * 1_000
    # Worker work is a fraction of the wall time; the rest is process start.
    assert (time.perf_counter() - started) * 1_000 > result.stats.duration_ms
