from __future__ import annotations

import json

from click.testing import CliRunner

from octop.cli.main import cli
from octop.infra.workbuddy.dependency_probe import probe_dependencies


def test_workbuddy_cel_command_returns_structured_result() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "workbuddy",
            "cel",
            "--expression",
            "inputs.value + 1",
            "--context",
            '{"inputs":{"value":2}}',
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["value"] == 3
    assert payload["stats"]["runner"] == "InterpretedRunner"


def test_workbuddy_cel_command_returns_controlled_error() -> None:
    result = CliRunner().invoke(
        cli,
        ["workbuddy", "cel", "--expression", '__import__("os")'],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "CEL_EVALUATION_ERROR"


def test_dependency_probe_fails_closed_when_external_services_are_unconfigured() -> None:
    payload = probe_dependencies({})
    by_name = {check["name"]: check for check in payload["checks"]}

    assert payload["status"] == "blocked"
    assert payload["policy"] == {"silent_fallback": False, "secrets_in_output": False}
    assert by_name["octop"]["status"] == "passed"
    assert by_name["cel"]["status"] == "passed"
    assert by_name["postgresql"]["status"] == "blocked"
    assert by_name["redis"]["status"] == "blocked"
    assert by_name["object_storage"]["status"] == "blocked"
    assert by_name["vault"]["status"] == "blocked"
    assert by_name["bge_m3"]["status"] == "blocked"
