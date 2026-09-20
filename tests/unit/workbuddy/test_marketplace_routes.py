"""Route-contract checks for the WorkBuddy marketplace router (no database needed).

The manifest is the published contract, so a route that disappears, changes its
method, or changes its success status is caught here even though installing a
template needs PostgreSQL (see test_workbuddy_marketplace_postgres.py).

Two normalizations are deliberate:

* the manifest writes path templates as ``{id}`` while FastAPI routes use
  descriptive names such as ``{template_id}``; both produce the same URL, so the
  comparison replaces every template with ``{}``;
* the platform review route is the one surface that must *not* resolve a tenant
  principal: its tenant comes from the path, because the reviewer is not a member
  of the submitting tenant, so it is checked against the platform audience.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from octop.api.routers import workbuddy_marketplace as mp
from octop.api.routers.workbuddy_identity import workbuddy_principal

MANIFEST = Path(__file__).resolve().parents[3] / "contracts" / "route-manifest.json"

_TEMPLATE = re.compile(r"\{[^}]*\}")
_PLATFORM_PREFIX = "/platform/submissions"


def _normalize(path: str) -> str:
    return _TEMPLATE.sub("{}", path)


def _manifest_routes() -> dict[tuple[str, str], int]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    routes: dict[tuple[str, str], int] = {}
    for entry in manifest["routes"]:
        path = entry["path"]
        if not path.startswith(("/marketplace", _PLATFORM_PREFIX)):
            continue
        routes[(entry["method"], _normalize(path))] = entry["success_status"]
    return routes


def _router_routes() -> dict[tuple[str, str], int]:
    routes: dict[tuple[str, str], int] = {}
    for route in mp.router.routes:
        # FastAPI leaves ``status_code`` unset (None) unless the decorator sets it,
        # and then answers 200.
        status = getattr(route, "status_code", None) or 200
        for method in getattr(route, "methods", []) or []:
            routes[(method, _normalize(route.path))] = status
    return routes


def _dependencies(route: object) -> set[object]:
    dependant = getattr(route, "dependant", None)
    return {dependency.call for dependency in (dependant.dependencies if dependant else [])}


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
    """An install must not answer 200 where the contract publishes 202."""
    manifest = _manifest_routes()
    router = _router_routes()

    mismatched = {
        key: {"router": router[key], "manifest": status}
        for key, status in manifest.items()
        if key in router and router[key] != status
    }
    assert not mismatched, mismatched


def test_every_tenant_route_requires_the_tenant_principal() -> None:
    """No tenant route may be reachable without a derived tenant principal."""
    unguarded = [
        (sorted(getattr(route, "methods", []) or []), route.path)
        for route in mp.router.routes
        if not route.path.startswith(_PLATFORM_PREFIX)
        and workbuddy_principal not in _dependencies(route)
    ]
    assert not unguarded, f"routes without the tenant principal: {unguarded}"


def test_the_review_route_requires_the_platform_audience() -> None:
    """Platform review is a separate audience; a tenant principal is not enough."""
    route = next(route for route in mp.router.routes if route.path.startswith(_PLATFORM_PREFIX))
    platform_dependency = mp._Platform.__metadata__[0].dependency  # type: ignore[attr-defined]
    dependencies = _dependencies(route)

    assert platform_dependency in dependencies
    assert workbuddy_principal not in dependencies


def test_the_installation_ledger_takes_its_tenant_from_the_principal_only() -> None:
    """The list is scoped by the authenticated principal, never by the request."""
    route = next(route for route in mp.router.routes if route.path == "/marketplace/installations")
    dependant = route.dependant
    named = {param.name for param in dependant.query_params} | {
        param.name for param in dependant.path_params
    }

    assert getattr(route, "methods", set()) == {"GET"}
    assert (getattr(route, "status_code", None) or 200) == 200
    assert "tenant_id" not in named
    assert workbuddy_principal in _dependencies(route)
