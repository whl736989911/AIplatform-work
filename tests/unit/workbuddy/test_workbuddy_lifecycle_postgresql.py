"""T25 acceptance against a live PostgreSQL: export, redeem, deletion, purge, restore.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``); the
database is dedicated to the suite and is reset here.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.workbuddy_lifecycle import WorkBuddyLifecycleRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy import lifecycle as policy

pytestmark = [pytest.mark.postgresql, requires_postgresql]

DAY = 86400


@pytest.fixture(scope="module")
def pool() -> Iterator[PostgresPool]:
    db = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"], min_size=1, max_size=4)
    with db.transaction() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        # The upstream migrations declare a pgvector column; the dedicated test
        # database must therefore provide the extension before they run.
        available = conn.execute(
            "SELECT 1 AS present FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if available is None:
            pytest.skip("pgvector is required by the upstream migrations")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def tenant(pool: PostgresPool) -> dict[str, str]:
    """One tenant-owned dataset per test.

    Deliberately no teardown: every lifecycle path appends to the append-only
    deletion ledger (and, after a purge, the tombstone), and both tables
    reference ``workbuddy_tenants`` with ``ON DELETE RESTRICT`` — a tenant that
    was exercised here is immortal by design.  The module-scoped ``pool``
    fixture resets the schema instead, as
    ``tests/integration/test_workbuddy_isolation_postgres.py`` does.  Per-test
    uniqueness (slug, username, invitation token hash) is what keeps the tests
    independent within a run.
    """
    now = int(time.time())
    suffix = uuid.uuid4().hex[:8]
    with workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn:
        user_id = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, 'not-a-real-hash', 'user', ?) RETURNING id
            """,
            (f"wb-lifecycle-{suffix}", now),
        ).fetchone()["id"]
        tenant_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO workbuddy_tenants
              (tenant_id, slug, slug_normalized, name, status, created_at, updated_at)
            VALUES (?, ?, ?, 'Lifecycle Tenant', 'active', ?, ?)
            """,
            (tenant_id, f"life-{suffix}", f"life-{suffix}", now, now),
        )
        conn.execute(
            """
            INSERT INTO workbuddy_tenant_members
              (tenant_id, user_id, role, status, joined_at, updated_at)
            VALUES (?, ?, 'owner', 'active', ?, ?)
            """,
            (tenant_id, user_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO workbuddy_invitations
              (tenant_id, email, email_normalized, token_hash, expires_at, created_at)
            VALUES (?, 'invitee@example.com', 'invitee@example.com', ?, ?, ?)
            """,
            (tenant_id, uuid.uuid4().hex + uuid.uuid4().hex, now + DAY, now),
        )
    return {"tenant_id": tenant_id, "user_id": str(user_id)}


def _signed_policy_env(tenant_id: str) -> dict[str, str]:
    now = int(time.time())
    document = {
        "version": policy.POLICY_VERSION,
        "tenant_id": tenant_id,
        "policy_id": "compliance-1",
        "approved_at": now - 60,
        "expires_at": now + 10 * DAY,
        "legal_basis": "customer-contract-termination",
        "retention_days": 90,
    }
    document["signature"] = policy.sign_policy(document, b"test-policy-key")
    return {
        policy.POLICY_ENV: base64.urlsafe_b64encode(json.dumps(document).encode()).decode(),
        policy.POLICY_KEY_ENV: "test-policy-key",
    }


def _verify_ledger(repo: WorkBuddyLifecycleRepo, tenant_id: str) -> list[str]:
    with repo.transaction(WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn:
        rows = repo.list_ledger_entries(conn, tenant_id=tenant_id, limit=500)
    entries = [
        policy.LedgerEntry(
            ledger_entry_id=str(row["ledger_entry_id"]),
            tenant_id=str(row["tenant_id"]),
            deletion_request_id=row["deletion_request_id"],
            sequence=int(row["sequence"]),
            entry_type=str(row["entry_type"]),
            payload=json.loads(str(row["payload_json"])),
            payload_sha256=str(row["payload_sha256"]),
            previous_sha256=row["previous_sha256"],
            entry_sha256=str(row["entry_sha256"]),
            created_at=int(row["created_at"]),
        )
        for row in rows
    ]
    assert policy.verify_ledger_chain(entries)
    return [entry.entry_type for entry in entries]


def test_export_is_redacted_verifiable_and_redeemed_once(
    pool: PostgresPool, tenant: dict[str, str]
) -> None:
    repo = WorkBuddyLifecycleRepo(pool)
    tenant_id = tenant["tenant_id"]
    issue = policy.start_tenant_export(repo, tenant_id=tenant_id, user_id=int(tenant["user_id"]))

    manifest = issue.job.manifest
    assert issue.job.status == "ready"
    assert issue.job.manifest_sha256 == policy.manifest_sha256(manifest)
    tables = {entry["name"]: entry for entry in manifest["tables"]}
    invitations = tables["workbuddy_invitations"]
    assert "token_hash" in invitations["excluded_columns"]
    assert "token_hash" not in invitations["columns"]
    assert all("credential" not in name and "secret" not in name for name in tables)

    download = policy.redeem_export(
        repo, tenant_id=tenant_id, user_id=int(tenant["user_id"]), token=issue.redeem_token
    )
    invitation_rows = next(
        item for item in download.tables if item["name"] == "workbuddy_invitations"
    )
    assert invitation_rows["rows"], "the tenant's own rows are exported"
    assert all("token_hash" not in row for row in invitation_rows["rows"])
    assert invitation_rows["content_sha256"] == policy.sha256_text(
        policy.canonical_json(invitation_rows["rows"])
    )

    with pytest.raises(OctopError) as replay:
        policy.redeem_export(
            repo, tenant_id=tenant_id, user_id=int(tenant["user_id"]), token=issue.redeem_token
        )
    assert replay.value.code is ErrorCode.EXPORT_REDEEM_CONSUMED


def test_download_challenge_reissues_the_frozen_redeem_challenge_once(
    pool: PostgresPool, tenant: dict[str, str]
) -> None:
    """Stage D against the real schema: credential -> challenge -> one download."""
    repo = WorkBuddyLifecycleRepo(pool)
    tenant_id = tenant["tenant_id"]
    user_id = int(tenant["user_id"])
    issue = policy.start_tenant_export(repo, tenant_id=tenant_id, user_id=user_id)

    credential = policy.issue_reauth_credential(repo, tenant_id=tenant_id, user_id=user_id)
    challenge = policy.issue_download_challenge(
        repo,
        tenant_id=tenant_id,
        user_id=user_id,
        export_job_id=issue.job.export_job_id,
        credential=credential.credential,
    )
    # The re-issued challenge inherits the window frozen at job creation.
    assert challenge.expires_at == issue.job.redeem_expires_at

    # The credential is single-use: only the digest is stored, and a replay is
    # refused before anything is minted.
    with repo.transaction(WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn:
        stored = repo.get_reauth_credential(
            conn,
            tenant_id=tenant_id,
            credential_sha256=policy.hash_redeem_token(credential.credential),
        )
    assert stored is not None
    assert stored["credential_sha256"] == policy.hash_redeem_token(credential.credential)
    assert credential.credential not in json.dumps(stored, default=str)
    with pytest.raises(OctopError) as reused:
        policy.issue_download_challenge(
            repo,
            tenant_id=tenant_id,
            user_id=user_id,
            export_job_id=issue.job.export_job_id,
            credential=credential.credential,
        )
    assert reused.value.code is ErrorCode.AUTH_INVALID_CREDENTIALS

    download = policy.redeem_export(
        repo, tenant_id=tenant_id, user_id=user_id, token=challenge.challenge
    )
    assert download.export_job_id == issue.job.export_job_id
    with pytest.raises(OctopError) as replay:
        policy.redeem_export(repo, tenant_id=tenant_id, user_id=user_id, token=challenge.challenge)
    assert replay.value.code is ErrorCode.EXPORT_REDEEM_CONSUMED

    # An export that was already downloaded never yields a second challenge.
    another = policy.issue_reauth_credential(repo, tenant_id=tenant_id, user_id=user_id)
    with pytest.raises(OctopError) as consumed:
        policy.issue_download_challenge(
            repo,
            tenant_id=tenant_id,
            user_id=user_id,
            export_job_id=issue.job.export_job_id,
            credential=another.credential,
        )
    assert consumed.value.code is ErrorCode.EXPORT_REDEEM_CONSUMED

    with repo.transaction(WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn:
        job = repo.get_export_job(conn, tenant_id=tenant_id, export_job_id=issue.job.export_job_id)
        entry_types = [
            str(row["entry_type"])
            for row in repo.list_ledger_entries(conn, tenant_id=tenant_id, limit=50)
        ]
    assert job is not None
    assert job["redeem_expires_at"] == issue.job.redeem_expires_at
    # Both issuances are on the record: the create-time token and the challenge.
    assert entry_types.count("export_redeem_issued") == 2


def test_deletion_requires_the_policy_and_cancels_only_inside_cooling_off(
    pool: PostgresPool, tenant: dict[str, str]
) -> None:
    repo = WorkBuddyLifecycleRepo(pool)
    tenant_id = tenant["tenant_id"]
    user_id = int(tenant["user_id"])

    with pytest.raises(OctopError) as blocked:
        policy.request_tenant_deletion(repo, tenant_id=tenant_id, user_id=user_id, environ={})
    assert blocked.value.code is ErrorCode.COMPLIANCE_GATE_CLOSED

    view = policy.request_tenant_deletion(
        repo, tenant_id=tenant_id, user_id=user_id, environ=_signed_policy_env(tenant_id)
    )
    assert view.stage == "cooling_off"
    assert view.cancellable

    with pytest.raises(OctopError) as stale_version:
        policy.cancel_tenant_deletion(
            repo,
            tenant_id=tenant_id,
            user_id=user_id,
            deletion_request_id=view.deletion_request_id,
            expected_version=view.version + 5,
        )
    assert stale_version.value.code is ErrorCode.DELETION_REQUEST_CONFLICT

    cancelled = policy.cancel_tenant_deletion(
        repo,
        tenant_id=tenant_id,
        user_id=user_id,
        deletion_request_id=view.deletion_request_id,
        expected_version=view.version,
    )
    assert cancelled.stage == "cancelled"


def test_purge_keeps_tombstone_evidence_and_blocks_resurrection(
    pool: PostgresPool, tenant: dict[str, str]
) -> None:
    repo = WorkBuddyLifecycleRepo(pool)
    tenant_id = tenant["tenant_id"]
    user_id = int(tenant["user_id"])
    now = int(time.time())
    view = policy.request_tenant_deletion(
        repo,
        tenant_id=tenant_id,
        user_id=user_id,
        environ=_signed_policy_env(tenant_id),
        now=now,
    )

    with pytest.raises(OctopError) as too_early:
        policy.cancel_tenant_deletion(
            repo,
            tenant_id=tenant_id,
            user_id=user_id,
            deletion_request_id=view.deletion_request_id,
            now=now + policy.COOLING_OFF_SECONDS + DAY,
        )
    assert too_early.value.code is ErrorCode.DELETION_CANCEL_WINDOW_CLOSED

    archived = policy.advance_deletion_lifecycle(repo, now=now + policy.COOLING_OFF_SECONDS + DAY)
    assert view.deletion_request_id in archived.archived

    purged = policy.advance_deletion_lifecycle(
        repo, now=now + policy.COOLING_OFF_SECONDS + policy.ARCHIVE_RETENTION_SECONDS + 2 * DAY
    )
    assert view.deletion_request_id in purged.purged

    with repo.transaction(WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn:
        tombstone = policy.tombstone_from_row(repo.get_tombstone(conn, tenant_id=tenant_id))
        tables = [name for name in repo.tenant_tables(conn) if not policy.purge_protects(name)]
        counts = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=tables)
    assert tombstone.is_valid()
    assert {name: count for name, count in counts.items() if count} == {}
    assert tombstone.purged_at > 0
    assert policy.verify_purge_coverage(repo, tenant_id=tenant_id) == {}

    entry_types = _verify_ledger(repo, tenant_id)
    for expected in (
        "deletion_requested",
        "archive_created",
        "usage_linkage_anonymized",
        "purge_completed",
    ):
        assert expected in entry_types

    with (
        pytest.raises(psycopg.Error),
        workbuddy_transaction(pool, WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn,
    ):
        conn.execute(
            "UPDATE workbuddy_deletion_ledger SET entry_type = 'tampered' WHERE tenant_id = ?",
            (tenant_id,),
        )

    with workbuddy_transaction(pool, WorkBuddyDbContext.platform(tenant_id=tenant_id)) as conn:
        conn.execute(
            """
            INSERT INTO workbuddy_invitations
              (tenant_id, email, email_normalized, token_hash, expires_at, created_at)
            VALUES (?, 'restored@example.com', 'restored@example.com', ?, ?, ?)
            """,
            (tenant_id, "a" * 64, now + DAY, now),
        )
    replay = policy.replay_deletion_ledger(repo, tenant_id=tenant_id)
    assert replay.action == policy.RESTORE_ACTION_REPURGE
    assert "workbuddy_invitations" in replay.reseeded_tables
    assert policy.verify_purge_coverage(repo, tenant_id=tenant_id) == {}
    assert "restore_replayed" in _verify_ledger(repo, tenant_id)
