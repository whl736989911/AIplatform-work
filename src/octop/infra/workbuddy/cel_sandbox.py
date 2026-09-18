"""Deterministic, process-isolated CEL evaluation for WorkBuddy workflows.

Only JSON values cross the process boundary.  The child exposes the CEL standard
library and caller-provided JSON activation; it never registers host functions.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

CEL_EXPRESSION_MAX_LENGTH = 2_000
CEL_AST_MAX_DEPTH = 64
CEL_EVALUATION_MAX_NODES = 100_000
CEL_EVALUATION_TIMEOUT_SECONDS = 1.0
CEL_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
CEL_WORKER_STARTUP_TIMEOUT_SECONDS = 10.0

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class _Pipe(Protocol):
    def poll(self, timeout: float = 0.0) -> bool: ...

    def recv(self) -> Any: ...

    def send(self, obj: object) -> None: ...

    def close(self) -> None: ...


class _ManagedProcess(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CELSandboxLimits:
    """Hard limits applied to a single CEL expression."""

    expression_length: int = CEL_EXPRESSION_MAX_LENGTH
    ast_depth: int = CEL_AST_MAX_DEPTH
    evaluation_nodes: int = CEL_EVALUATION_MAX_NODES
    evaluation_timeout_seconds: float = CEL_EVALUATION_TIMEOUT_SECONDS
    memory_bytes: int = CEL_MEMORY_LIMIT_BYTES
    worker_startup_timeout_seconds: float = CEL_WORKER_STARTUP_TIMEOUT_SECONDS


DEFAULT_CEL_SANDBOX_LIMITS = CELSandboxLimits()


@dataclass(frozen=True, slots=True)
class CELSandboxStats:
    """Evidence emitted for each successful evaluation."""

    expression_length: int
    ast_depth: int
    ast_nodes: int
    evaluated_nodes: int
    duration_ms: float
    memory_limit_enforced: bool
    runner: str = "InterpretedRunner"


@dataclass(frozen=True, slots=True)
class CELSandboxResult:
    value: JSONValue
    value_type: str
    stats: CELSandboxStats

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "value_type": self.value_type, "stats": asdict(self.stats)}


class CELSandboxError(RuntimeError):
    """Controlled CEL failure safe to return to an API or CLI caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class _CostLimitExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _EvaluationBudget:
    maximum: int
    visited: int = 0

    def consume(self) -> None:
        self.visited += 1
        if self.visited > self.maximum:
            raise _CostLimitExceeded(f"CEL evaluation exceeded {self.maximum} node visits")


def _apply_memory_limit(memory_bytes: int) -> bool:
    """Apply an address-space limit where the operating system supports it."""

    if os.name != "posix":
        return False

    import importlib

    resource: Any = importlib.import_module("resource")
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    return True


def _ast_metrics(root: Any) -> tuple[int, int]:
    """Return (node count, maximum depth) without recursive Python calls."""

    from lark import Tree

    nodes = 0
    maximum_depth = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, Tree):
            continue
        nodes += 1
        maximum_depth = max(maximum_depth, depth)
        stack.extend((child, depth + 1) for child in item.children if isinstance(child, Tree))
    return nodes, maximum_depth


def _preload_cel_runtime() -> None:
    """Import celpy before the worker reports ready.

    The import is charged to ``worker_startup_timeout_seconds``; without this the
    first evaluation on a cold machine pays for it out of the (much smaller)
    evaluation timeout and a correct evaluation is reported as ``CEL_TIMEOUT``.
    """
    from celpy import Environment, InterpretedRunner, celtypes  # noqa: F401
    from celpy.adapter import CELJSONEncoder, json_to_cel  # noqa: F401
    from celpy.celparser import CELParseError  # noqa: F401
    from celpy.evaluation import Activation, CELEvalError, Context, Evaluator  # noqa: F401


def _evaluate_in_worker(
    expression: str, context: dict[str, JSONValue], limits: CELSandboxLimits
) -> dict[str, Any]:
    from celpy import Environment, InterpretedRunner, celtypes
    from celpy.adapter import CELJSONEncoder, json_to_cel
    from celpy.celparser import CELParseError
    from celpy.evaluation import Activation, CELEvalError, Context, Evaluator

    class BudgetedEvaluator(Evaluator):
        def __init__(self, ast: Any, activation: Activation, budget: _EvaluationBudget) -> None:
            super().__init__(ast=ast, activation=activation)
            self._budget = budget

        def _visit_tree(self, tree: Any) -> Any:
            self._budget.consume()
            return super()._visit_tree(tree)

        def sub_evaluator(self, ast: Any) -> BudgetedEvaluator:
            return BudgetedEvaluator(ast, activation=self.activation, budget=self._budget)

    class BudgetedInterpretedRunner(InterpretedRunner):
        node_limit = limits.evaluation_nodes
        nodes_visited = 0

        def evaluate(self, activation: Context) -> celtypes.Value:
            budget = _EvaluationBudget(self.node_limit)
            evaluator = BudgetedEvaluator(
                ast=self.ast,
                activation=self.new_activation(),
                budget=budget,
            )
            try:
                return evaluator.evaluate(activation)
            finally:
                self.nodes_visited = budget.visited

    started = time.perf_counter()
    environment = Environment(runner_class=BudgetedInterpretedRunner)
    try:
        ast = environment.compile(expression)
    except CELParseError as exc:
        raise CELSandboxError("CEL_SYNTAX_ERROR", str(exc)[:500]) from exc

    ast_nodes, ast_depth = _ast_metrics(ast)
    if ast_depth > limits.ast_depth:
        raise CELSandboxError(
            "CEL_AST_DEPTH_EXCEEDED",
            f"CEL AST depth {ast_depth} exceeds the limit {limits.ast_depth}",
        )
    if ast_nodes > limits.evaluation_nodes:
        raise CELSandboxError(
            "CEL_COST_EXCEEDED",
            f"CEL AST node count {ast_nodes} exceeds the limit {limits.evaluation_nodes}",
        )

    runner = environment.program(ast)
    if not isinstance(runner, BudgetedInterpretedRunner):
        raise CELSandboxError("CEL_SANDBOX_ERROR", "CEL interpreter runner was not selected")

    activation = {name: json_to_cel(value) for name, value in context.items()}
    try:
        value = runner.evaluate(activation)
    except _CostLimitExceeded as exc:
        raise CELSandboxError("CEL_COST_EXCEEDED", str(exc)) from exc
    except CELEvalError as exc:
        raise CELSandboxError("CEL_EVALUATION_ERROR", str(exc)[:500]) from exc
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise CELSandboxError("CEL_EVALUATION_ERROR", str(exc)[:500]) from exc

    try:
        encoded = json.dumps(value, cls=CELJSONEncoder, allow_nan=False, separators=(",", ":"))
        native_value = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise CELSandboxError(
            "CEL_OUTPUT_NOT_JSON", "CEL result is not a finite JSON value"
        ) from exc

    return {
        "value": native_value,
        "value_type": type(value).__name__,
        "expression_length": len(expression),
        "ast_depth": ast_depth,
        "ast_nodes": ast_nodes,
        "evaluated_nodes": runner.nodes_visited,
        "duration_ms": (time.perf_counter() - started) * 1_000,
    }


def _worker(connection: _Pipe, limits: CELSandboxLimits) -> None:
    memory_limit_enforced = False
    try:
        try:
            memory_limit_enforced = _apply_memory_limit(limits.memory_bytes)
        except (OSError, ValueError) as exc:
            connection.send(
                {
                    "status": "error",
                    "code": "CEL_MEMORY_LIMIT_UNAVAILABLE",
                    "message": f"cannot enforce CEL memory limit: {exc}",
                }
            )
            return

        _preload_cel_runtime()

        connection.send({"status": "ready", "memory_limit_enforced": memory_limit_enforced})
        request = connection.recv()
        result = _evaluate_in_worker(request["expression"], request["context"], limits)
        connection.send(
            {
                "status": "ok",
                "memory_limit_enforced": memory_limit_enforced,
                "result": result,
            }
        )
    except CELSandboxError as exc:
        connection.send({"status": "error", **exc.to_dict()})
    except MemoryError:
        connection.send(
            {
                "status": "error",
                "code": "CEL_MEMORY_LIMIT_EXCEEDED",
                "message": "CEL worker exceeded its memory limit",
            }
        )
    except (EOFError, OSError):
        return
    except BaseException as exc:  # child boundary: never leak a raw worker exception
        connection.send(
            {
                "status": "error",
                "code": "CEL_SANDBOX_ERROR",
                "message": f"CEL worker failed: {type(exc).__name__}",
            }
        )
    finally:
        connection.close()


def _stop_process(process: _ManagedProcess) -> None:
    if not process.is_alive():
        process.join(timeout=0.2)
        return
    process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)


def _receive(connection: _Pipe, timeout: float, code: str, message: str) -> dict[str, Any]:
    if not connection.poll(timeout):
        raise CELSandboxError(code, message)
    try:
        payload = connection.recv()
    except EOFError as exc:
        raise CELSandboxError("CEL_SANDBOX_ERROR", "CEL worker exited without a result") from exc
    if not isinstance(payload, dict) or "status" not in payload:
        raise CELSandboxError("CEL_SANDBOX_ERROR", "CEL worker returned an invalid response")
    return payload


def evaluate_cel(
    expression: str,
    context: dict[str, JSONValue] | None = None,
    *,
    limits: CELSandboxLimits = DEFAULT_CEL_SANDBOX_LIMITS,
) -> CELSandboxResult:
    """Evaluate CEL in a fresh process with deterministic inputs and bounded work."""

    if not isinstance(expression, str) or not expression:
        raise CELSandboxError("CEL_INVALID_EXPRESSION", "CEL expression must be a non-empty string")
    if len(expression) > limits.expression_length:
        raise CELSandboxError(
            "CEL_EXPRESSION_TOO_LONG",
            f"CEL expression length exceeds the limit {limits.expression_length}",
        )
    if context is None:
        context = {}
    if not isinstance(context, dict) or any(not isinstance(key, str) for key in context):
        raise CELSandboxError("CEL_INVALID_CONTEXT", "CEL context must be a JSON object")
    try:
        normalized_context = json.loads(json.dumps(context, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise CELSandboxError(
            "CEL_INVALID_CONTEXT", "CEL context must contain finite JSON values"
        ) from exc

    process_context = multiprocessing.get_context("spawn")
    parent, child = process_context.Pipe(duplex=True)
    process = process_context.Process(target=_worker, args=(child, limits), daemon=True)
    try:
        process.start()
        child.close()
        ready = _receive(
            parent,
            limits.worker_startup_timeout_seconds,
            "CEL_WORKER_STARTUP_TIMEOUT",
            "CEL worker did not become ready",
        )
        if ready["status"] != "ready":
            raise CELSandboxError(
                str(ready.get("code", "CEL_SANDBOX_ERROR")),
                str(ready.get("message", "CEL worker failed to start")),
            )

        parent.send({"expression": expression, "context": normalized_context})
        response = _receive(
            parent,
            limits.evaluation_timeout_seconds,
            "CEL_TIMEOUT",
            f"CEL evaluation exceeded {limits.evaluation_timeout_seconds:g} seconds",
        )
        if response["status"] != "ok":
            raise CELSandboxError(
                str(response.get("code", "CEL_SANDBOX_ERROR")),
                str(response.get("message", "CEL evaluation failed")),
            )

        raw = response["result"]
        stats = CELSandboxStats(
            expression_length=int(raw["expression_length"]),
            ast_depth=int(raw["ast_depth"]),
            ast_nodes=int(raw["ast_nodes"]),
            evaluated_nodes=int(raw["evaluated_nodes"]),
            duration_ms=float(raw["duration_ms"]),
            memory_limit_enforced=bool(response["memory_limit_enforced"]),
        )
        return CELSandboxResult(
            value=raw["value"],
            value_type=str(raw["value_type"]),
            stats=stats,
        )
    finally:
        parent.close()
        _stop_process(process)
