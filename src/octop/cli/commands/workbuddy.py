"""WorkBuddy contract and dependency probes."""

from __future__ import annotations

import json
from typing import Any

import click

from octop.infra.workbuddy import CELSandboxError, evaluate_cel, probe_dependencies


@click.group()
def workbuddy() -> None:
    """Verify WorkBuddy runtime contracts without starting the server."""


@workbuddy.command("cel")
@click.option("--expression", "-e", required=True, help="CEL expression to evaluate.")
@click.option(
    "--context",
    "context_json",
    default="{}",
    show_default=True,
    help="JSON object exposed as the CEL activation.",
)
def cel(expression: str, context_json: str) -> None:
    """Evaluate one expression in the bounded interpreter-only CEL sandbox."""

    try:
        parsed: Any = json.loads(context_json)
    except json.JSONDecodeError as exc:
        _fail("CEL_INVALID_CONTEXT", f"context is not valid JSON: {exc.msg}")
        return
    if not isinstance(parsed, dict):
        _fail("CEL_INVALID_CONTEXT", "context must be a JSON object")
        return

    try:
        result = evaluate_cel(expression, parsed)
    except CELSandboxError as exc:
        _fail(exc.code, exc.message)
        return
    click.echo(json.dumps({"status": "passed", **result.to_dict()}, ensure_ascii=False))


@workbuddy.command("dependencies")
def dependencies() -> None:
    """Probe locked components and configured external dependencies as secret-free JSON."""

    result = probe_dependencies()
    click.echo(json.dumps(result, ensure_ascii=False))
    if result["status"] == "failed":
        raise click.exceptions.Exit(2)
    if result["status"] == "blocked":
        raise click.exceptions.Exit(3)


def _fail(code: str, message: str) -> None:
    click.echo(
        json.dumps(
            {"status": "failed", "error": {"code": code, "message": message}},
            ensure_ascii=False,
        ),
        err=True,
    )
    raise click.exceptions.Exit(2)
