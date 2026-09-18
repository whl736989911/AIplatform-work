"""Route-contract checks for the WorkBuddy workflow router (no database needed).

The manifest is the published contract, so a route that disappears, changes
method, or changes its success status is caught here even though the behaviour
itself needs PostgreSQL (see test_workbuddy_workflows_postgres.py).

Two normalizations are deliberate:

* the manifest writes path templates as ``{id}`` while FastAPI routes use
  descriptive names such as ``{workflow_id}``; both produce the same URL, so the
  comparison replaces every template with ``{}``;
* paths owned by other modules (execute, improvement-proposals,
  trigger-registrations) belong to the runtime, proposal, and knowledge
  routers, so they are excluded here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from octop.api.routers import workbuddy_workflows

MANIFEST = Path(__file__).resolve().parents[3] / "contracts" / "route-manifest.json"

_OTHER_MODULE_SUFFIXES = ("/execute", "/improvement-proposals", "/trigger-registrations")
_TEMPLATE = re.compile(r"\{[^}]*\}")


def _normalize(path: str) -> str:
    return _TEMPLATE.sub("{}", path)


def _manifest_routes() -> dict[tuple[str, str], int]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    routes: dict[tuple[str, str], int] = {}
    for entry in manifest["routes"]:
        path = entry["path"]
        if not path.startswith(("/workflows", "/workflow-definitions")):
            continue
        if any(suffix in path for suffix in _OTHER_MODULE_SUFFIXES):
            continue
        routes[(entry["method"], _normalize(path))] = entry["success_status"]
    return routes


def _router_routes() -> dict[tuple[str, str], int]:
    routes: dict[tuple[str, str], int] = {}
    for route in workbuddy_workflows.router.routes:
        # FastAPI leaves ``status_code`` unset (None) unless the decorator sets it,
        # and then answers 200.
        status = getattr(route, "status_code", None) or 200
        for method in getattr(route, "methods", []) or []:
            routes[(method, _normalize(route.path))] = status
    return routes


def test_router_covers_every_manifest_route() -> None:
    manifest = _manifest_routes()
    router = _router_routes()

    assert not sorted(set(manifest) - set(router)), (
        f"manifest routes not implemented: {sorted(set(manifest) - set(router))}"
    )
    assert not sorted(set(router) - set(manifest)), (
        f"routes not in the manifest: {sorted(set(router) - set(manifest))}"
    )


def test_success_statuses_match_the_manifest() -> None:
    """A create/publish path must not silently answer 200 instead of 201."""
    manifest = _manifest_routes()
    router = _router_routes()

    mismatched = {
        key: {"router": router[key], "manifest": status}
        for key, status in manifest.items()
        if key in router and router[key] != status
    }
    assert not mismatched, mismatched


def test_every_route_requires_the_tenant_principal() -> None:
    """No workflow route may be reachable without a derived tenant principal."""
    from octop.api.routers.workbuddy_identity import workbuddy_principal

    unguarded = []
    for route in workbuddy_workflows.router.routes:
        dependant = getattr(route, "dependant", None)
        names = {
            getattr(dependency.call, "__name__", "")
            for dependency in (dependant.dependencies if dependant else [])
        }
        if workbuddy_principal.__name__ not in names:
            unguarded.append((sorted(getattr(route, "methods", []) or []), route.path))
    assert not unguarded, f"routes without the tenant principal: {unguarded}"
