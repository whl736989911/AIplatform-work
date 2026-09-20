"""The published node metadata must be the schema, not a second copy of it.

An editor renders node forms from ``GET /workflow-definitions/metadata``; if that
document listed a field the compiler does not enforce — or missed a required one
— every form it builds would produce definitions the compiler refuses. These
tests derive the same facts from ``contracts/workflow-v1.schema.json`` and from
the compiler's constants, and compare.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from octop.api.deps import get_server
from octop.api.routers import workbuddy_workflows
from octop.api.routers.workbuddy_identity import workbuddy_principal
from octop.infra.errors import OctopError
from octop.infra.workbuddy.node_metadata import definition_metadata
from octop.infra.workbuddy.workflow_compiler import (
    CEL_REFERENCE_NAMESPACES,
    NODE_TYPES,
    REFERENCE_SYNTAX,
    TEMPLATE_FIELD_PATHS,
    WORKFLOW_COMPILER_VERSION,
    WORKFLOW_SCHEMA_VERSION,
    workflow_schema,
)


def _schema_node_branches() -> dict[str, dict[str, Any]]:
    """``{node type: config schema}`` straight from the checked-in schema."""
    branches: dict[str, dict[str, Any]] = {}
    for branch in workflow_schema()["definitions"]["node"]["allOf"]:
        node_type = branch["if"]["properties"]["type"]["const"]
        branches[node_type] = branch["then"]["properties"]["config"]
    return branches


def test_node_types_and_config_fields_come_from_the_schema() -> None:
    """Every derived node type, required key and field name equals the schema's."""
    metadata = definition_metadata()
    branches = _schema_node_branches()

    assert {item["type"] for item in metadata["node_types"]} == set(branches) == set(NODE_TYPES)

    for item in metadata["node_types"]:
        config = branches[item["type"]]
        assert item["required"] == list(config["required"]), item["type"]
        assert item["optional"] == sorted(set(config["properties"]) - set(config["required"]))
        assert {field["name"] for field in item["config_fields"]} == set(config["properties"])
        for field in item["config_fields"]:
            assert field["required"] is (field["name"] in config["required"]), field["name"]
        # A node type reads templates in exactly the field the compiler scans.
        expected_template = TEMPLATE_FIELD_PATHS.get(item["type"])
        assert item["template_fields"] == ([expected_template] if expected_template else [])


def test_config_field_bounds_are_the_schema_bounds() -> None:
    """Bounds travel with the field: a form must not offer an invalid value."""
    metadata = definition_metadata()
    branches = _schema_node_branches()
    by_type = {item["type"]: item for item in metadata["node_types"]}

    approval = by_type["approval"]["config_fields"]
    timeout = next(field for field in approval if field["name"] == "timeout_hours")
    assert (timeout["minimum"], timeout["maximum"]) == (1, 168)
    assert timeout["default"] == 24
    approvers = next(field for field in approval if field["name"] == "approver_user_ids")
    assert approvers["items"] == {"type": "string", "format": "uuid"}

    llm = by_type["llm"]["config_fields"]
    temperature = next(field for field in llm if field["name"] == "temperature")
    assert (temperature["minimum"], temperature["maximum"]) == (0, 2)

    # The schema's own bounds, not a remembered copy of them.
    schema_timeout = branches["approval"]["properties"]["timeout_hours"]
    assert timeout["maximum"] == schema_timeout["maximum"] == 168
    assert timeout["minimum"] == schema_timeout["minimum"] == 1


def test_input_types_and_trigger_types_come_from_the_schema() -> None:
    metadata = definition_metadata()
    schema = workflow_schema()
    input_type = schema["definitions"]["inputDeclaration"]["properties"]["type"]

    assert metadata["inputs"]["types"] == input_type["enum"]
    assert metadata["inputs"]["required"] == ["type"]
    assert {field["name"] for field in metadata["inputs"]["fields"]} == set(
        schema["definitions"]["inputDeclaration"]["properties"]
    )
    # ``required`` defaults to false in the schema, and the form must not demand it.
    required_field = next(
        field for field in metadata["inputs"]["fields"] if field["name"] == "required"
    )
    assert required_field["required"] is False
    assert required_field["default"] is False

    trigger_types = {item["type"] for item in metadata["trigger_types"]}
    assert trigger_types == set(schema["definitions"]["trigger"]["properties"]["type"]["enum"])
    cron = next(item for item in metadata["trigger_types"] if item["type"] == "cron")
    assert cron["required"] == ["cron_expression", "timezone"]


def test_reference_syntax_matches_the_compiler() -> None:
    metadata = definition_metadata()
    syntax = metadata["reference_syntax"]

    assert syntax["references"] == [
        {"kind": kind, "syntax": value} for kind, value in REFERENCE_SYNTAX.items()
    ]
    assert (
        syntax["identifier"]["pattern"]
        == (workflow_schema()["definitions"]["identifier"]["pattern"])
    )
    assert syntax["template"]["examples"] == [
        f"{{{{ {value} }}}}" for value in REFERENCE_SYNTAX.values()
    ]
    assert syntax["template"]["fields"] == dict(TEMPLATE_FIELD_PATHS)
    assert [item["namespace"] for item in metadata["cel_reference_namespaces"]] == list(
        CEL_REFERENCE_NAMESPACES
    )
    assert [item["syntax"] for item in metadata["cel_reference_namespaces"]] == [
        value[1] for value in CEL_REFERENCE_NAMESPACES.values()
    ]


def test_metadata_reports_the_versions_it_was_derived_from() -> None:
    metadata = definition_metadata()

    assert metadata["schema_version"] == WORKFLOW_SCHEMA_VERSION
    assert metadata["compiler_version"] == WORKFLOW_COMPILER_VERSION
    assert metadata["output"]["required"] == ["format", "destination"]
    assert [field["name"] for field in metadata["limits"]["fields"]] == [
        "max_steps",
        "max_duration_sec",
        "max_output_bytes",
        "timeout_per_step_sec",
    ]


class _Principal:
    """The metadata route reads the derived tenant only; nothing tenant-specific is returned."""

    tenant_id = "6f1a2c3d-4e5b-4a7c-8d9e-0f1a2b3c4d5e"
    user_id = 7
    user = SimpleNamespace(id=7, username="wb-member")


async def test_metadata_route_answers_the_derived_document() -> None:
    app = FastAPI()
    app.include_router(workbuddy_workflows.router, prefix="/api/v1")

    @app.exception_handler(OctopError)
    async def _octop(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    app.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=object())
    )
    app.dependency_overrides[workbuddy_principal] = _Principal

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/workflow-definitions/metadata")

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    data = body["data"]
    assert {item["type"] for item in data["node_types"]} == set(NODE_TYPES)
    assert data["compiler_version"] == WORKFLOW_COMPILER_VERSION
    assert data["reference_syntax"]["template"]["max_placeholders"] == 32
    # The route serves exactly what the module derives, in one JSON-safe shape.
    assert data == json.loads(json.dumps(definition_metadata()))


def test_metadata_route_requires_the_tenant_principal() -> None:
    """The contract is static, but it is still only served to a member."""
    route = next(
        route
        for route in workbuddy_workflows.router.routes
        if isinstance(route, APIRoute) and route.path.endswith("/workflow-definitions/metadata")
    )

    assert route.methods is not None and "GET" in route.methods
    assert route.status_code is None, "an unset status code answers 200, as the manifest says"
    calls = {dependency.call for dependency in route.dependant.dependencies}
    assert workbuddy_principal in calls


@pytest.mark.parametrize("node_type", sorted(NODE_TYPES))
def test_every_node_type_the_compiler_accepts_is_described(node_type: str) -> None:
    """A node type the compiler knows but the metadata omits is unusable in a form."""
    metadata = definition_metadata()

    assert node_type in {item["type"] for item in metadata["node_types"]}
