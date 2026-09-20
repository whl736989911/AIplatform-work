"""Route-contract checks for the proposals, lifecycle and re-auth routers.

These three surfaces share the failure mode this batch repaired: the frozen
manifest published a route (`POST /improvement-proposals/{id}/reviewers`,
`POST /auth/reauthenticate`, `POST /exports/{id}/download-challenge`), the code
never implemented it, and nothing compared the two — so the first symptom was a
404 in a user's browser. The comparison lives here now.

Two normalizations match test_marketplace_routes.py: the manifest writes path
templates as ``{id}`` while FastAPI routes use descriptive names, and FastAPI
leaves ``status_code`` unset unless the decorator sets it (then it answers 200).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from octop.api.routers import workbuddy_identity as ident
from octop.api.routers import workbuddy_lifecycle as life
from octop.api.routers import workbuddy_proposals as prop
from octop.api.routers.workbuddy_identity import workbuddy_principal

MANIFEST = Path(__file__).resolve().parents[3] / "contracts" / "route-manifest.json"

_TEMPLATE = re.compile(r"\{[^}]*\}")
_PREFIXES = ("/improvement-proposals", "/exports", "/tenants")
_EXACT = frozenset({"/auth/reauthenticate"})

#: The routes this batch implemented, as the contract publishes them.
_BATCH_ROUTES = (
    ("POST", "/improvement-proposals/{}/reviewers"),
    ("POST", "/auth/reauthenticate"),
    ("POST", "/exports/{}/download-challenge"),
)


def _normalize(path: str) -> str:
    return _TEMPLATE.sub("{}", path)


def _in_scope(path: str) -> bool:
    return path.startswith(_PREFIXES) or path in _EXACT


def _manifest_routes() -> dict[tuple[str, str], int]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {
        (entry["method"], _normalize(entry["path"])): entry["success_status"]
        for entry in manifest["routes"]
        if _in_scope(entry["path"])
    }


def _iter_router_routes() -> list[Any]:
    routes = []
    for module in (prop, life, ident):
        routes.extend(route for route in module.router.routes if _in_scope(route.path))
    return routes


def _router_routes() -> dict[tuple[str, str], int]:
    routes: dict[tuple[str, str], int] = {}
    for route in _iter_router_routes():
        status = getattr(route, "status_code", None) or 200
        for method in getattr(route, "methods", []) or []:
            routes[(method, _normalize(route.path))] = status
    return routes


def _dependency_calls(route: Any) -> set[Any]:
    """Every callable in the route's dependency tree, nested ones included.

    Composed dependencies such as ``Depends(require_workbuddy_admin())`` hide the
    principal resolver one level down, so a single-level walk would report a
    guarded route as unguarded.
    """
    found: set[Any] = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        dependency = stack.pop()
        found.add(dependency.call)
        stack.extend(getattr(dependency, "dependencies", []) or [])
    return found


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
    """A reviewer assignment or a challenge must not answer 202 where the contract says 200."""
    manifest = _manifest_routes()
    router = _router_routes()
    mismatched = {
        key: {"router": router[key], "manifest": manifest[key]}
        for key in set(manifest) & set(router)
        if manifest[key] != router[key]
    }
    assert not mismatched, mismatched


def test_the_batch_routes_exist_and_resolve_the_tenant_principal() -> None:
    """The three repaired routes answer their published success status and stay tenant-scoped."""
    manifest = _manifest_routes()
    for key in _BATCH_ROUTES:
        assert key in manifest, f"{key} missing from the manifest"
    router = _router_routes()
    for key in _BATCH_ROUTES:
        assert key in router, f"{key} missing from the routers"
        assert router[key] == manifest[key] == 200

    by_key: dict[tuple[str, str], Any] = {}
    for route in _iter_router_routes():
        for method in getattr(route, "methods", []) or []:
            by_key[(method, _normalize(route.path))] = route
    for key in _BATCH_ROUTES:
        calls = _dependency_calls(by_key[key])
        assert workbuddy_principal in calls, f"{key} does not resolve the caller's tenant"


def test_no_route_in_this_slice_is_public() -> None:
    """Tenant and credential routes are never reachable without a dependency."""
    unguarded = [
        (sorted(getattr(route, "methods", []) or []), route.path)
        for route in _iter_router_routes()
        if not _dependency_calls(route)
    ]
    assert not unguarded, f"routes without any dependency: {unguarded}"
