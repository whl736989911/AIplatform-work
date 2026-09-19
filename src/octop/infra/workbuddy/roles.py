"""Tenant-scoped role vocabulary shared by the API boundary, the governance
repository, and the runtime service.

The Octop platform role is a separate axis: these roles only say what a member
may do inside their own WorkBuddy tenant.
"""

from __future__ import annotations

TENANT_ADMIN_ROLES = frozenset({"owner", "admin"})

__all__ = ["TENANT_ADMIN_ROLES"]
