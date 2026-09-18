"""WorkBuddy lifecycle policy: redaction, manifest, redeem, gate, ledger, purge."""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest

from octop.infra.db.pool import SqlitePool
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    WorkBuddyPostgresRequiredError,
    workbuddy_transaction,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy import lifecycle as policy


def _signed_env(payload: dict, key: str = "policy-key") -> dict[str, str]:
    document = dict(payload)
    document["signature"] = policy.sign_policy(document, key.encode("utf-8"))
    encoded = base64.urlsafe_b64encode(json.dumps(document).encode("utf-8")).decode("ascii")
    return {policy.POLICY_ENV: encoded, policy.POLICY_KEY_ENV: key}


def _live_policy(**overrides: object) -> dict:
    now = int(time.time())
    payload = {
        "version": policy.POLICY_VERSION,
        "tenant_id": str(uuid.uuid4()),
        "policy_id": "policy-1",
        "approved_at": now - 60,
        "expires_at": now + 86400,
        "legal_basis": "contract-end",
        "retention_days": 90,
    }
    payload.update(overrides)
    return payload


# ── export redaction ────────────────────────────────────────────────────────


def test_exportable_columns_drop_secret_and_hash_material() -> None:
    kept, dropped = policy.exportable_columns(
        [
            "membership_id",
            "display_name",
            "email",
            "password_hash",
            "token_hash",
            "secret_ref",
            "vault_ref",
            "encrypted_payload",
            "approval_challenge",
            "download_hash",
            "definition_sha256",
            "hmac_key",
            "salt_secret",
            "event_count",
        ]
    )
    assert kept == ("membership_id", "display_name", "email", "event_count")
    assert set(dropped) == {
        "password_hash",
        "token_hash",
        "secret_ref",
        "vault_ref",
        "encrypted_payload",
        "approval_challenge",
        "download_hash",
        "definition_sha256",
        "hmac_key",
        "salt_secret",
    }


def test_redact_row_keeps_relational_ids_and_json_types() -> None:
    membership_id = uuid.uuid4()
    row = {
        "membership_id": membership_id,
        "tenant_id": uuid.uuid4(),
        "role": "admin",
        "token_hash": "deadbeef",
        "salt_secret": b"\x00\x01",
    }
    redacted = policy.redact_row(row)
    assert redacted["membership_id"] == str(membership_id)
    assert redacted["role"] == "admin"
    assert "token_hash" not in redacted
    assert "salt_secret" not in redacted


def test_secret_bearing_tables_are_excluded_whole() -> None:
    assert (
        policy.excluded_table_category("workbuddy_connector_credentials") == "credential_metadata"
    )
    assert policy.excluded_table_category("secrets") == "secret_material"
    assert policy.excluded_table_category("vault_leases") == "vault_reference"
    assert policy.excluded_table_category("workbuddy_tenant_members") is None


def test_manifest_is_canonical_and_verifiable() -> None:
    table = policy.ExportTable(
        name="workbuddy_tenant_members",
        row_count=2,
        columns=("membership_id", "role"),
        excluded_columns=("token_hash",),
        content_sha256="a" * 64,
    )
    excluded = policy.ExportTableExclusion(name="secrets", category="secret_material")
    manifest = policy.build_export_manifest(
        tenant_id="t-1",
        export_job_id="j-1",
        created_at=1234,
        tables=[table],
        excluded_tables=[excluded],
    )
    assert manifest["totals"] == {"tables": 1, "rows": 2, "excluded_tables": 1}
    assert manifest["tables"][0]["content_sha256"] == "a" * 64
    assert manifest["excluded_tables"] == [
        {"name": "secrets", "category": "secret_material", "included": False}
    ]
    # The recorded digest is recomputable from the returned manifest itself.
    assert policy.manifest_sha256(manifest) == policy.sha256_json(manifest)
    assert policy.manifest_sha256(dict(reversed(list(manifest.items())))) == policy.manifest_sha256(
        manifest
    )


# ── redeem tokens ───────────────────────────────────────────────────────────


def test_redeem_token_is_hash_only_and_single_use() -> None:
    issued = policy.issue_redeem_token()
    assert issued.raw != issued.token_sha256
    assert issued.token_sha256 == policy.hash_redeem_token(issued.raw)
    assert policy.redeem_token_failure(None) is ErrorCode.EXPORT_REDEEM_INVALID
    live = {"consumed_at": None, "revoked_at": None, "expires_at": 1_000}
    assert policy.redeem_token_failure(live, now=999) is None
    assert policy.redeem_token_failure(live, now=1_000) is ErrorCode.EXPORT_REDEEM_EXPIRED
    assert (
        policy.redeem_token_failure({**live, "consumed_at": 900}, now=999)
        is ErrorCode.EXPORT_REDEEM_CONSUMED
    )
    assert (
        policy.redeem_token_failure({**live, "revoked_at": 900}, now=999)
        is ErrorCode.EXPORT_REDEEM_INVALID
    )


def test_redeem_window_is_fixed_at_job_creation() -> None:
    created = 1_700_000_000
    assert policy.redeem_window_expires_at(created) == created + 72 * 3600


# ── compliance gate ─────────────────────────────────────────────────────────


def test_gate_is_closed_without_a_signed_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(policy.POLICY_ENV, raising=False)
    monkeypatch.delenv(policy.POLICY_KEY_ENV, raising=False)
    with pytest.raises(OctopError) as failure:
        policy.load_compliance_policy("tenant-1")
    assert failure.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED
    assert failure.value.status == 503


def test_gate_refuses_unsigned_mismatched_and_expired_policies() -> None:
    now = int(time.time())
    tenant = "tenant-1"
    with pytest.raises(OctopError) as unsigned:
        policy.load_compliance_policy(tenant, environ={policy.POLICY_ENV: "not-base64"})
    assert unsigned.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED

    tampered = _signed_env(_live_policy(tenant_id=tenant))
    tampered[policy.POLICY_ENV] = tampered[policy.POLICY_ENV][:-4] + "AAAA"
    with pytest.raises(OctopError) as bad_signature:
        policy.load_compliance_policy(tenant, environ=tampered)
    assert bad_signature.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED

    other_tenant = _signed_env(_live_policy(tenant_id="tenant-2"))
    with pytest.raises(OctopError) as mismatch:
        policy.load_compliance_policy(tenant, environ=other_tenant)
    assert mismatch.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED

    expired = _signed_env(
        _live_policy(tenant_id=tenant, approved_at=now - 900, expires_at=now - 10)
    )
    with pytest.raises(OctopError) as stale:
        policy.load_compliance_policy(tenant, environ=expired, now=now)
    assert stale.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED


def test_signed_policy_authorises_only_its_tenant() -> None:
    tenant = "tenant-1"
    environ = _signed_env(_live_policy(tenant_id=tenant, policy_id="p-9"))
    loaded = policy.load_compliance_policy(tenant, environ=environ)
    assert loaded.covers(tenant)
    assert not loaded.covers("tenant-2")
    assert loaded.as_ledger_payload()["policy_sha256"] == loaded.digest
    wildcard = policy.load_compliance_policy(
        "tenant-3", environ=_signed_env(_live_policy(tenant_id="*"))
    )
    assert wildcard.covers("tenant-3")


# ── ledger, tombstone and restore replay ────────────────────────────────────


def _entry(
    sequence: int, previous: str | None, entry_type: str = "deletion_requested"
) -> policy.LedgerEntry:
    payload = {"deletion_request_id": f"r-{sequence}"}
    payload_sha256 = policy.ledger_payload_sha256(payload)
    created_at = 1_700_000_000 + sequence
    return policy.LedgerEntry(
        ledger_entry_id=str(uuid.uuid4()),
        tenant_id="tenant-1",
        deletion_request_id=f"r-{sequence}",
        sequence=sequence,
        entry_type=entry_type,
        payload=payload,
        payload_sha256=payload_sha256,
        previous_sha256=previous,
        entry_sha256=policy.ledger_entry_sha256(
            tenant_id="tenant-1",
            sequence=sequence,
            entry_type=entry_type,
            payload_sha256=payload_sha256,
            previous_sha256=previous,
            created_at=created_at,
        ),
        created_at=created_at,
    )


def test_ledger_chain_detects_tampering() -> None:
    first = _entry(1, None)
    second = _entry(2, first.entry_sha256, "purge_completed")
    assert policy.verify_ledger_chain([first, second])
    assert not policy.verify_ledger_chain([_entry(1, "deadbeef")])
    broken = policy.LedgerEntry(**{**second.__dict__, "payload": {"deletion_request_id": "other"}})
    assert not policy.verify_ledger_chain([first, broken])
    assert not policy.verify_ledger_chain([second])


def test_tombstone_digest_detects_tampering() -> None:
    args = {
        "tenant_id": "tenant-1",
        "deletion_request_id": "r-1",
        "purged_at": 1_700_000_000,
        "policy_sha256": "b" * 64,
        "ledger_head_sha256": "c" * 64,
        "ledger_entry_count": 4,
        "purged_tables": 3,
        "purged_rows": 12,
    }
    tombstone = policy.Tombstone(
        **args,
        archive_sha256="d" * 64,
        archive_row_total=9,
        usage_linkage_sha256=None,
        evidence_tables=tuple(sorted(policy.PURGE_EVIDENCE_TABLES)),
        tombstone_sha256=policy.tombstone_digest(**args),
    )
    assert tombstone.is_valid()
    tampered = policy.Tombstone(**{**tombstone.__dict__, "purged_rows": 0})
    assert not tampered.is_valid()


def test_restore_replay_plan_repurges_only_tombstoned_tenants() -> None:
    clean = policy.build_restore_replay_plan(
        tenant_id="tenant-1", tombstone=None, ledger_entries=0, reseeded_tables=()
    )
    assert clean.action == policy.RESTORE_ACTION_NONE
    assert not clean.must_repurge

    tombstone = policy.Tombstone(
        tenant_id="tenant-2",
        deletion_request_id="r-2",
        purged_at=1_700_000_000,
        policy_sha256="b" * 64,
        ledger_head_sha256="c" * 64,
        ledger_entry_count=5,
        archive_sha256="d" * 64,
        archive_row_total=4,
        usage_linkage_sha256=None,
        purged_tables=2,
        purged_rows=7,
        evidence_tables=("workbuddy_deletion_ledger",),
        tombstone_sha256="e" * 64,
    )
    plan = policy.build_restore_replay_plan(
        tenant_id="tenant-2",
        tombstone=tombstone,
        ledger_entries=5,
        reseeded_tables=["workbuddy_tenant_members", "workbuddy_tenant_members"],
    )
    assert plan.must_repurge
    assert plan.tombstoned
    assert plan.reseeded_tables == ("workbuddy_tenant_members",)
    assert plan.ledger_head_sha256 == "c" * 64


def test_purge_order_deletes_children_before_parents() -> None:
    tables = ["workbuddy_export_jobs", "workbuddy_export_artifacts", "workbuddy_tenant_members"]
    dependencies = {"workbuddy_export_artifacts": {"workbuddy_export_jobs"}}
    order = policy.order_purge_tables(tables, dependencies)
    assert order.index("workbuddy_export_artifacts") < order.index("workbuddy_export_jobs")
    assert set(order) == set(tables)


def test_purge_order_refuses_cycles() -> None:
    with pytest.raises(OctopError) as failure:
        policy.order_purge_tables(["a", "b"], {"a": {"b"}, "b": {"a"}})
    assert failure.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE


def test_purge_plan_batches_a_foreign_key_cycle() -> None:
    plan = policy.plan_tenant_purge(["a", "b", "c"], {"a": {"b"}, "b": {"a"}})
    assert plan.order == ("c",)
    assert plan.batched == ("a", "b")
    assert set(plan.tables) == {"a", "b", "c"}


def test_purge_plan_treats_self_references_as_leaves() -> None:
    plan = policy.plan_tenant_purge(
        ["workbuddy_departments", "workbuddy_tenant_members"],
        {"workbuddy_departments": {"workbuddy_departments"}},
    )
    assert plan.batched == ()
    assert set(plan.order) == {"workbuddy_departments", "workbuddy_tenant_members"}
    assert policy.order_purge_tables(["t"], {"t": {"t"}}) == ["t"]


def test_purge_evidence_tables_are_never_deleted() -> None:
    assert policy.purge_protects("workbuddy_deletion_ledger")
    assert policy.purge_protects("workbuddy_tenant_tombstones")
    assert policy.purge_protects("workbuddy_deletion_requests")
    assert not policy.purge_protects("workbuddy_tenant_members")


# ── usage linkage ───────────────────────────────────────────────────────────


def test_usage_links_are_salt_bound_and_irreversible() -> None:
    first_ref, first_salt = policy.new_usage_salt()
    second_ref, second_salt = policy.new_usage_salt()
    assert first_ref != second_ref
    digest = policy.usage_subject_sha256(first_salt, "user", "42")
    assert policy.verify_usage_subject(digest)
    assert digest != policy.usage_subject_sha256(second_salt, "user", "42")
    assert digest != policy.usage_subject_sha256(first_salt, "agent", "42")
    with pytest.raises(OctopError) as bad_kind:
        policy.usage_subject_sha256(first_salt, "unknown", "42")
    assert bad_kind.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT


# ── deletion timeline ───────────────────────────────────────────────────────


def test_deletion_cancel_is_limited_to_the_cooling_off_window() -> None:
    now = 1_700_000_000
    row = {
        "deletion_request_id": "r-1",
        "stage": "cooling_off",
        "requested_at": now - 100,
        "cooling_off_ends_at": now + 100,
        "purge_due_at": None,
        "purged_at": None,
    }
    assert policy.deletion_timeline(row, now=now, legal_hold_active=False).cancellable
    assert not policy.deletion_timeline(row, now=now + 100, legal_hold_active=False).cancellable
    archived = {**row, "stage": "archived", "purge_due_at": now + 1000}
    assert not policy.deletion_timeline(archived, now=now, legal_hold_active=False).cancellable


# ── SQLite can never serve tenant lifecycle data ─────────────────────────────


def test_workbuddy_transaction_fails_closed_on_sqlite(tmp_path: object) -> None:
    pool = SqlitePool(tmp_path / "octop.db")  # type: ignore[operator]
    try:
        with (
            pytest.raises(WorkBuddyPostgresRequiredError),
            workbuddy_transaction(pool, WorkBuddyDbContext.for_tenant(str(uuid.uuid4()))),
        ):
            raise AssertionError("SQLite must never open a WorkBuddy transaction")
    finally:
        pool.close()


def test_lifecycle_windows_match_the_contract() -> None:
    assert policy.COOLING_OFF_SECONDS == 30 * 86400
    assert policy.ARCHIVE_RETENTION_SECONDS == 90 * 86400
    assert policy.REDEEM_TTL_SECONDS == 72 * 3600
