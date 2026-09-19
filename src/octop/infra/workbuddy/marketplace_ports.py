"""Tenant-side adapters for the marketplace service's injection ports.

:class:`~octop.infra.workbuddy.marketplace.MarketplaceService` depends on three
narrow ports instead of on repositories, so install and upgrade run against the
same logic in the API and in tests.  This module is the production binding:
each adapter delegates to an existing WorkBuddy repository and fails closed,
naming the slice it needs, rather than answering "no" for a deployment that
simply does not have that slice yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from octop.infra.db.repos.workbuddy_catalog import WorkBuddyCatalogRepo
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.marketplace import WorkflowVersionRef

#: ``workbuddy_workflow_versions.origin`` accepts
#: ``save|rollback|proposal|promotion|import`` (migration 017).  A template that
#: an operator installs enters the tenant from outside it — that is what
#: ``import`` names; the other values describe edits made inside the tenant.
INSTALL_ORIGIN = "import"

#: Row status that means "still usable" for a member or a connector credential.
ACTIVE_STATUS = "active"

__all__ = [
    "INSTALL_ORIGIN",
    "CatalogCapabilityAdapter",
    "TenantBindingAdapter",
    "WorkflowInstallAdapter",
]


class CatalogCapabilityAdapter:
    """``TenantCapabilityPort`` over the catalog slice."""

    def __init__(self, repo: WorkBuddyCatalogRepo) -> None:
        self._repo = repo

    def tenant_capabilities(self, tenant_id: str) -> Any:
        """The tenant's approved tool/model revisions, or ``None`` when unset."""
        return self._repo.get_capabilities(tenant_id)

    def tool_keys(self, tenant_id: str) -> Mapping[str, tuple[str, str]]:
        """Published tool revision id -> ``(adapter_key, tool_key)``.

        Published revisions are platform-wide, so the mapping is not tenant
        scoped: the service intersects it with the tenant's approved ids.
        """
        return {
            str(row.tool_revision_id).lower(): (row.adapter_key, row.tool_key)
            for row in self._repo.list_tool_revisions()
        }

    def model_keys(self, tenant_id: str) -> Mapping[str, str]:
        """Published model revision id -> ``model_key`` (same platform-wide shape)."""
        return {
            str(row.model_revision_id).lower(): row.model_key
            for row in self._repo.list_model_revisions()
        }


class TenantBindingAdapter:
    """``TenantBindingPort``: same-tenant existence checks for rebound references."""

    def __init__(
        self,
        *,
        identity: WorkBuddyIdentityRepo,
        catalog: WorkBuddyCatalogRepo,
        db: Any,
    ) -> None:
        self._identity = identity
        self._catalog = catalog
        self._db = db

    def knowledge_base_exists(self, tenant_id: str, kb_id: str) -> bool:
        """True only for a live knowledge base of this tenant.

        Knowledge bases belong to the knowledge slice (migration 019).  A
        deployment without it cannot satisfy a template that needs one, and the
        install has to say so: answering ``False`` here would read as "that id is
        wrong" and hide a missing slice behind a binding error.
        """
        if not self._knowledge_tables_deployed():
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "the knowledge slice is not deployed on this control plane",
                details={"capability_kind": "knowledge_base"},
            )
        with workbuddy_transaction(self._db, WorkBuddyDbContext.for_tenant(tenant_id)) as conn:
            row = conn.execute(
                "SELECT 1 AS present FROM workbuddy_knowledge_bases "
                "WHERE tenant_id = ? AND kb_id = ? AND archived_at IS NULL",
                (str(tenant_id), str(kb_id)),
            ).fetchone()
        return row is not None

    def approver_is_active_member(self, tenant_id: str, member_id: str) -> bool:
        member = self._identity.get_member(tenant_id, member_id)
        return member is not None and str(member.get("status")) == ACTIVE_STATUS

    def credential_is_active(self, tenant_id: str, credential_id: str) -> bool:
        credential = self._catalog.get_credential(tenant_id, credential_id)
        return credential is not None and str(getattr(credential, "status", "")) == ACTIVE_STATUS

    def _knowledge_tables_deployed(self) -> bool:
        with workbuddy_transaction(self._db, WorkBuddyDbContext.platform()) as conn:
            row = conn.execute(
                "SELECT to_regclass('public.workbuddy_knowledge_bases') AS relation"
            ).fetchone()
        return row is not None and row["relation"] is not None


class WorkflowInstallAdapter:
    """``WorkflowInstallPort`` over the workflows slice.

    An install creates the workflow and its first version; an upgrade appends a
    version to the installed workflow.  Both run inside the caller's transaction
    (``conn``), so a failed plan write leaves neither a version nor a half
    installation behind, and the append compare-and-swaps the workflow revision
    the caller read.
    """

    def __init__(self, repo: WorkBuddyWorkflowRepo) -> None:
        self._repo = repo

    def create_workflow_version(
        self,
        *,
        tenant_id: str,
        name: str,
        description: str,
        definition: Mapping[str, Any],
        definition_sha256: str,
        user_id: int | None,
        member_id: str,
        change_summary: str,
        conn: Any = None,
    ) -> WorkflowVersionRef:
        bundle = self._repo.create_workflow(
            tenant_id,
            name=name,
            description=description,
            definition=definition,
            definition_sha256=definition_sha256,
            created_by_user_id=user_id,
            created_by_membership_id=member_id,
            change_summary=change_summary,
            origin=INSTALL_ORIGIN,
            conn=conn,
        )
        return WorkflowVersionRef(
            workflow_id=str(bundle.workflow.workflow_id),
            workflow_version_id=str(bundle.version.workflow_version_id),
            version_number=bundle.version.version_number,
        )

    def append_workflow_version(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        definition: Mapping[str, Any],
        definition_sha256: str,
        user_id: int | None,
        member_id: str,
        change_summary: str,
        conn: Any = None,
    ) -> WorkflowVersionRef:
        workflow = self._repo.get_workflow(tenant_id, workflow_id, conn=conn)
        if workflow is None:
            raise OctopError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the installation's workflow no longer exists",
                details={"workflow_id": str(workflow_id)},
            )
        bundle = self._repo.save_version(
            tenant_id,
            workflow_id,
            definition=definition,
            definition_sha256=definition_sha256,
            expected_revision=workflow.revision,
            created_by_user_id=user_id,
            created_by_membership_id=member_id,
            change_summary=change_summary,
            origin=INSTALL_ORIGIN,
            conn=conn,
        )
        return WorkflowVersionRef(
            workflow_id=str(bundle.workflow.workflow_id),
            workflow_version_id=str(bundle.version.workflow_version_id),
            version_number=bundle.version.version_number,
        )
