"""WorkBuddy tenant lifecycle policy: exports, redeem tokens, deletion, purge.

This module is the decision layer for the tenant lifecycle; SQL lives in
:mod:`octop.infra.db.repos.workbuddy_lifecycle` and HTTP wiring in
:mod:`octop.api.routers.workbuddy_lifecycle`.

Guarantees kept here:

* an export is only defined by what a *redaction rule set* allows.  Tables that
  hold secret material and their metadata (``*secret*``, ``*vault*``,
  ``*credential*``, ``*password*``) are excluded whole, and columns that hold
  password/invite/approval/download hashes, Vault references, encrypted payloads
  or secret metadata are dropped before a row leaves its source table, so no
  export payload can contain one;
* the export manifest is canonical JSON whose sha256 is recorded with the job, so
  a consumer can recompute the digest over the tables it received;
* a redeem token is a 256-bit random value that only ever exists as its sha256
  in the database, is bound to one export job and one 72h window, and is
  consumed by a single conditional UPDATE (CAS);
* deletion is fail-closed: it requires a signed compliance policy bound to the
  tenant and inside its validity window, otherwise the API answers
  ``COMPLIANCE_GATE_CLOSED`` and no job is queued;
* the deletion ledger is hash chained, append-only and the only authority for
  restore replay: a restored snapshot for a tombstoned tenant is re-purged
  before the service can serve it again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.log_redaction import register_secret

__all__ = [
    "ARCHIVE_RETENTION_SECONDS",
    "COOLING_OFF_SECONDS",
    "EXPORT_SCHEMA",
    "LEDGER_ENTRY_TYPES",
    "MAX_EXPORT_ROWS_PER_TABLE",
    "POLICY_ENV",
    "POLICY_KEY_ENV",
    "PURGE_EVIDENCE_TABLES",
    "REDACTION_RULES_VERSION",
    "REDEEM_TTL_SECONDS",
    "CompliancePolicy",
    "DeletionTimeline",
    "ExportTable",
    "ExportTableExclusion",
    "LedgerEntry",
    "PurgePlan",
    "RedeemToken",
    "RestoreReplayPlan",
    "Tombstone",
    "build_export_manifest",
    "build_restore_replay_plan",
    "canonical_json",
    "column_is_exportable",
    "deletion_timeline",
    "excluded_table_category",
    "exportable_columns",
    "hash_redeem_token",
    "issue_redeem_token",
    "ledger_entry_sha256",
    "ledger_payload_sha256",
    "load_compliance_policy",
    "manifest_sha256",
    "order_purge_tables",
    "plan_tenant_purge",
    "policy_digest",
    "purge_protects",
    "redeem_token_failure",
    "redeem_window_expires_at",
    "redact_row",
    "require_compliance_policy",
    "sha256_json",
    "sha256_text",
    "sign_policy",
    "tombstone_digest",
    "usage_subject_sha256",
    "verify_ledger_chain",
]

# ── lifecycle windows ───────────────────────────────────────────────────────

COOLING_OFF_SECONDS = 30 * 86400
ARCHIVE_RETENTION_SECONDS = 90 * 86400
REDEEM_TTL_SECONDS = 72 * 3600
EXPORT_SCHEMA = "workbuddy.export.v1"
REDACTION_RULES_VERSION = 1
MAX_EXPORT_ROWS_PER_TABLE = 50_000

# ── canonical JSON and digests ──────────────────────────────────────────────


def canonical_json(value: Any) -> str:
    """Deterministic JSON text used for every digest in this package."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


# ── export redaction ────────────────────────────────────────────────────────

# Whole tables that may hold secret material or its metadata never reach an
# export, not even as an empty table projection.
_EXCLUDED_TABLE_PATTERNS = ("secret", "vault", "credential", "password")
_EXCLUDED_TABLE_CATEGORY = {
    "secret": "secret_material",
    "vault": "vault_reference",
    "credential": "credential_metadata",
    "password": "password_material",
}

# Column level rules: exact names and fragments that identify hashes of
# credentials, secret references, encrypted payloads and challenge material.
_EXCLUDED_COLUMN_NAMES = frozenset(
    {
        "token",
        "token_hash",
        "access_token",
        "refresh_token",
        "raw_token",
        "redeem_token",
        "session_token",
        "password",
        "password_hash",
        "passwd",
        "salt",
        "salt_secret",
        "secret",
        "secret_ref",
        "secret_json",
        "credential",
        "credential_json",
        "vault_ref",
        "private_key",
        "api_key",
        "apikey",
        "signing_key",
        "signing_secret",
        "hmac_key",
        "nonce",
        "ciphertext",
        "encrypted",
        "encrypted_payload",
        "approval_hash",
        "download_hash",
        "grant_hash",
        "challenge",
        "approval_challenge",
        "download_challenge",
    }
)
_EXCLUDED_COLUMN_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "vault",
    "ciphertext",
    "encrypted",
    "private_key",
    "api_key",
    "apikey",
    "challenge",
    "hmac",
    "nonce",
    "signing_key",
)
# Any hash-looking suffix is dropped as well: invite, approval, download and
# token digests all end this way, and an export consumer never needs them.
_EXCLUDED_HASH_SUFFIXES = ("_hash", "_sha256", "_sha512", "_digest", "_sum")


def excluded_table_category(table_name: str) -> str | None:
    """Category of secret-bearing metadata, or ``None`` when the table is exportable."""
    lowered = table_name.strip().lower()
    for pattern in _EXCLUDED_TABLE_PATTERNS:
        if pattern in lowered:
            return _EXCLUDED_TABLE_CATEGORY[pattern]
    return None


def column_is_exportable(column_name: str) -> bool:
    lowered = column_name.strip().lower()
    if lowered in _EXCLUDED_COLUMN_NAMES:
        return False
    if any(fragment in lowered for fragment in _EXCLUDED_COLUMN_FRAGMENTS):
        return False
    return not lowered.endswith(_EXCLUDED_HASH_SUFFIXES)


def exportable_columns(columns: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split declared columns into ``(exported, excluded)`` preserving order."""
    kept: list[str] = []
    dropped: list[str] = []
    for column in columns:
        (kept if column_is_exportable(column) else dropped).append(column)
    return tuple(kept), tuple(dropped)


def json_safe(value: Any) -> Any:
    """Convert a database value into something canonical JSON can carry."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Binary columns are storage detail, never export content.
        return None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)


def redact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Drop every non-exportable column from one source row."""
    return {
        str(column): json_safe(value)
        for column, value in row.items()
        if column_is_exportable(str(column))
    }


# ── export manifest ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExportTable:
    """One exported table: the digest covers exactly the rows that were written."""

    name: str
    row_count: int
    columns: tuple[str, ...]
    excluded_columns: tuple[str, ...]
    content_sha256: str

    def as_manifest_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "columns": list(self.columns),
            "excluded_columns": list(self.excluded_columns),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class ExportTableExclusion:
    """A table (and therefore its secret metadata) that is not part of the export."""

    name: str
    category: str

    def as_manifest_entry(self) -> dict[str, Any]:
        return {"name": self.name, "category": self.category, "included": False}


def build_export_manifest(
    *,
    tenant_id: str,
    export_job_id: str,
    created_at: int,
    tables: Sequence[ExportTable],
    excluded_tables: Sequence[ExportTableExclusion],
) -> dict[str, Any]:
    entries = [table.as_manifest_entry() for table in sorted(tables, key=lambda t: t.name)]
    exclusions = [
        entry.as_manifest_entry() for entry in sorted(excluded_tables, key=lambda e: e.name)
    ]
    return {
        "schema": EXPORT_SCHEMA,
        "redaction_rules_version": REDACTION_RULES_VERSION,
        "tenant_id": tenant_id,
        "export_job_id": export_job_id,
        "created_at": created_at,
        "tables": entries,
        "excluded_tables": exclusions,
        "totals": {
            "tables": len(entries),
            "rows": sum(entry["row_count"] for entry in entries),
            "excluded_tables": len(exclusions),
        },
        "content_sha256": sha256_json(entries),
    }


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256_json(dict(manifest))


# ── one-time redeem tokens ──────────────────────────────────────────────────

_REDEEM_TOKEN_BYTES = 32


@dataclass(frozen=True)
class RedeemToken:
    """A freshly minted redeem token: only ``token_sha256`` may be persisted."""

    raw: str
    token_sha256: str


def issue_redeem_token() -> RedeemToken:
    raw = secrets.token_urlsafe(_REDEEM_TOKEN_BYTES)
    register_secret(raw)
    return RedeemToken(raw=raw, token_sha256=hash_redeem_token(raw))


def hash_redeem_token(raw: str) -> str:
    return sha256_text(raw.strip())


def redeem_window_expires_at(job_created_at: int) -> int:
    """The 72h redeem window is fixed when the export job is created."""
    return int(job_created_at) + REDEEM_TTL_SECONDS


_REDEEM_FAILURES: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.EXPORT_REDEEM_INVALID: "export redeem token is not valid",
        ErrorCode.EXPORT_REDEEM_CONSUMED: "export redeem token was already consumed",
        ErrorCode.EXPORT_REDEEM_EXPIRED: "export redeem token has expired",
    }
)


def redeem_token_failure(
    row: Mapping[str, Any] | None,
    *,
    now: int | None = None,
) -> ErrorCode | None:
    """Classify a stored token row; ``None`` means the token may be consumed."""
    if row is None:
        return ErrorCode.EXPORT_REDEEM_INVALID
    moment = int(now if now is not None else time.time())
    if row.get("consumed_at") is not None:
        return ErrorCode.EXPORT_REDEEM_CONSUMED
    if row.get("revoked_at") is not None:
        return ErrorCode.EXPORT_REDEEM_INVALID
    expires_at = row.get("expires_at")
    if expires_at is None or int(expires_at) <= moment:
        return ErrorCode.EXPORT_REDEEM_EXPIRED
    return None


def redeem_token_error(code: ErrorCode) -> OctopError:
    return OctopError(code, _REDEEM_FAILURES[code])


# ── compliance policy (fail-closed deletion gate) ───────────────────────────

POLICY_ENV = "WORKBUDDY_COMPLIANCE_POLICY"
POLICY_KEY_ENV = "WORKBUDDY_COMPLIANCE_POLICY_KEY"
POLICY_VERSION = 1
_WILDCARD_TENANT = "*"

_COMPLIANCE_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "missing": "no signed compliance policy is configured for this deployment",
        "malformed": "the configured compliance policy is malformed",
        "key_missing": "the compliance policy signing key is not configured",
        "signature": "the compliance policy signature does not verify",
        "tenant": "the compliance policy is not bound to this tenant",
        "not_approved": "the compliance policy does not approve tenant deletion",
        "expired": "the compliance policy is outside its validity window",
        "version": "the compliance policy schema version is not supported",
    }
)


def compliance_gate_closed(reason: str) -> OctopError:
    message = _COMPLIANCE_REASONS[reason]
    return OctopError(ErrorCode.COMPLIANCE_GATE_CLOSED, message, details={"reason": reason})


@dataclass(frozen=True)
class CompliancePolicy:
    """A signed deletion approval for one tenant (or for the whole deployment)."""

    tenant_id: str
    policy_id: str
    version: int
    approved_at: int
    expires_at: int
    legal_basis: str
    retention_days: int
    digest: str
    signature_sha256: str

    def covers(self, tenant_id: str) -> bool:
        return self.tenant_id in (_WILDCARD_TENANT, str(tenant_id))

    def is_live(self, now: int) -> bool:
        return self.approved_at <= now < self.expires_at

    def as_ledger_payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_sha256": self.digest,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "legal_basis": self.legal_basis,
            "retention_days": self.retention_days,
        }


def policy_digest(payload: Mapping[str, Any]) -> str:
    """Digest of the signed payload, excluding the signature itself."""
    return sha256_json({key: value for key, value in payload.items() if key != "signature"})


def sign_policy(payload: Mapping[str, Any], key: bytes) -> str:
    """Operator helper: base64url HMAC-SHA256 over the canonical payload."""
    body = canonical_json({key: value for key, value in payload.items() if key != "signature"})
    mac = hmac.new(key, body.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def _decode_policy_document(raw: str, key_raw: str | None) -> dict[str, Any]:
    if not key_raw or not key_raw.strip():
        raise compliance_gate_closed("key_missing")
    try:
        encoded = raw.strip()
        document = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise compliance_gate_closed("malformed") from exc
    if not isinstance(document, dict):
        raise compliance_gate_closed("malformed")
    signature = document.get("signature")
    if not isinstance(signature, str) or not signature:
        raise compliance_gate_closed("malformed")
    expected = sign_policy(document, key_raw.strip().encode("utf-8"))
    if not hmac.compare_digest(expected, signature):
        raise compliance_gate_closed("signature")
    return document


def _policy_from_document(
    document: Mapping[str, Any], tenant_id: str, now: int
) -> CompliancePolicy:
    try:
        version = int(document["version"])
        policy = CompliancePolicy(
            tenant_id=str(document["tenant_id"]),
            policy_id=str(document["policy_id"]),
            version=version,
            approved_at=int(document["approved_at"]),
            expires_at=int(document["expires_at"]),
            legal_basis=str(document["legal_basis"]),
            retention_days=int(document.get("retention_days", 90)),
            digest=policy_digest(document),
            signature_sha256=sha256_text(str(document["signature"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise compliance_gate_closed("malformed") from exc
    if version != POLICY_VERSION:
        raise compliance_gate_closed("version")
    if not policy.covers(tenant_id):
        raise compliance_gate_closed("tenant")
    if policy.retention_days < 0 or policy.expires_at <= policy.approved_at:
        raise compliance_gate_closed("malformed")
    if not policy.is_live(now):
        raise compliance_gate_closed("expired")
    return policy


def load_compliance_policy(
    tenant_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    now: int | None = None,
) -> CompliancePolicy:
    """Return the tenant's signed policy or raise ``COMPLIANCE_GATE_CLOSED``.

    A deployment with no policy, no signing key, a bad signature, an expired
    policy or a policy bound to another tenant cannot delete data: there is no
    implicit approval and no "queue it anyway" path.
    """
    env = environ if environ is not None else _os_environ()
    raw = str(env.get(POLICY_ENV) or "").strip()
    if not raw:
        raise compliance_gate_closed("missing")
    document = _decode_policy_document(raw, env.get(POLICY_KEY_ENV))
    moment = int(now if now is not None else time.time())
    return _policy_from_document(document, str(tenant_id), moment)


def require_compliance_policy(
    tenant_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    now: int | None = None,
) -> CompliancePolicy:
    return load_compliance_policy(tenant_id, environ=environ, now=now)


def _os_environ() -> Mapping[str, str]:
    import os  # noqa: PLC0415 - keep module import cheap and side-effect free

    return os.environ


# ── append-only deletion ledger ─────────────────────────────────────────────

LEDGER_ENTRY_TYPES = frozenset(
    {
        "export_requested",
        "export_redeem_issued",
        "export_redeemed",
        "export_expired",
        "deletion_requested",
        "deletion_cancelled",
        "legal_hold_placed",
        "legal_hold_released",
        "archive_created",
        "usage_linkage_anonymized",
        "purge_completed",
        "restore_replayed",
    }
)


def ledger_payload_sha256(payload: Mapping[str, Any] | None) -> str:
    return sha256_json(dict(payload or {}))


def ledger_entry_sha256(
    *,
    tenant_id: str,
    sequence: int,
    entry_type: str,
    payload_sha256: str,
    previous_sha256: str | None,
    created_at: int,
) -> str:
    return sha256_json(
        {
            "tenant_id": str(tenant_id),
            "sequence": int(sequence),
            "entry_type": entry_type,
            "payload_sha256": payload_sha256,
            "previous_sha256": previous_sha256,
            "created_at": int(created_at),
        }
    )


@dataclass(frozen=True)
class LedgerEntry:
    ledger_entry_id: str
    tenant_id: str
    deletion_request_id: str | None
    sequence: int
    entry_type: str
    payload: dict[str, Any]
    payload_sha256: str
    previous_sha256: str | None
    entry_sha256: str
    created_at: int


def verify_ledger_chain(entries: Sequence[LedgerEntry]) -> bool:
    """True when the chain is contiguous, ordered and internally consistent."""
    previous: str | None = None
    expected_sequence = 1
    for entry in entries:
        if entry.sequence != expected_sequence:
            return False
        if entry.previous_sha256 != previous:
            return False
        if entry.payload_sha256 != ledger_payload_sha256(entry.payload):
            return False
        expected = ledger_entry_sha256(
            tenant_id=entry.tenant_id,
            sequence=entry.sequence,
            entry_type=entry.entry_type,
            payload_sha256=entry.payload_sha256,
            previous_sha256=entry.previous_sha256,
            created_at=entry.created_at,
        )
        if expected != entry.entry_sha256:
            return False
        previous = entry.entry_sha256
        expected_sequence += 1
    return True


# ── tombstone and restore replay ────────────────────────────────────────────


def tombstone_digest(
    *,
    tenant_id: str,
    deletion_request_id: str,
    purged_at: int,
    policy_sha256: str,
    ledger_head_sha256: str,
    ledger_entry_count: int,
    purged_tables: int,
    purged_rows: int,
) -> str:
    return sha256_json(
        {
            "tenant_id": str(tenant_id),
            "deletion_request_id": str(deletion_request_id),
            "purged_at": int(purged_at),
            "policy_sha256": policy_sha256,
            "ledger_head_sha256": ledger_head_sha256,
            "ledger_entry_count": int(ledger_entry_count),
            "purged_tables": int(purged_tables),
            "purged_rows": int(purged_rows),
        }
    )


@dataclass(frozen=True)
class Tombstone:
    tenant_id: str
    deletion_request_id: str
    purged_at: int
    policy_sha256: str
    ledger_head_sha256: str
    ledger_entry_count: int
    archive_sha256: str | None
    archive_row_total: int | None
    usage_linkage_sha256: str | None
    purged_tables: int
    purged_rows: int
    evidence_tables: tuple[str, ...]
    tombstone_sha256: str

    def is_valid(self) -> bool:
        expected = tombstone_digest(
            tenant_id=self.tenant_id,
            deletion_request_id=self.deletion_request_id,
            purged_at=self.purged_at,
            policy_sha256=self.policy_sha256,
            ledger_head_sha256=self.ledger_head_sha256,
            ledger_entry_count=self.ledger_entry_count,
            purged_tables=self.purged_tables,
            purged_rows=self.purged_rows,
        )
        return hmac.compare_digest(expected, self.tombstone_sha256)


RESTORE_ACTION_NONE = "none"
RESTORE_ACTION_REPURGE = "repurge"


@dataclass(frozen=True)
class RestoreReplayPlan:
    """What restore must do with a tenant snapshot before the service serves it."""

    tenant_id: str
    action: str
    tombstoned: bool
    purged_at: int | None
    ledger_head_sha256: str | None
    ledger_entry_count: int
    reseeded_tables: tuple[str, ...]
    evidence_tables: tuple[str, ...]

    @property
    def must_repurge(self) -> bool:
        return self.action == RESTORE_ACTION_REPURGE


def build_restore_replay_plan(
    *,
    tenant_id: str,
    tombstone: Tombstone | None,
    ledger_entries: int,
    reseeded_tables: Sequence[str],
) -> RestoreReplayPlan:
    """Decide the restore obligation for ``tenant_id`` from the ledger facts.

    A tombstoned tenant whose snapshot still carries rows must be re-purged
    before readiness: the ledger is the authority, not the restored copy.
    """
    if tombstone is None:
        return RestoreReplayPlan(
            tenant_id=str(tenant_id),
            action=RESTORE_ACTION_NONE,
            tombstoned=False,
            purged_at=None,
            ledger_head_sha256=None,
            ledger_entry_count=int(ledger_entries),
            reseeded_tables=(),
            evidence_tables=(),
        )
    return RestoreReplayPlan(
        tenant_id=str(tenant_id),
        action=RESTORE_ACTION_REPURGE,
        tombstoned=True,
        purged_at=tombstone.purged_at,
        ledger_head_sha256=tombstone.ledger_head_sha256,
        ledger_entry_count=int(ledger_entries),
        reseeded_tables=tuple(sorted({str(name) for name in reseeded_tables})),
        evidence_tables=tombstone.evidence_tables,
    )


# ── deletion timeline ───────────────────────────────────────────────────────

_STAGE_COOLING_OFF = "cooling_off"
_STAGE_CANCELLED = "cancelled"
_STAGE_ARCHIVED = "archived"
_STAGE_PURGED = "purged"


@dataclass(frozen=True)
class DeletionTimeline:
    deletion_request_id: str
    stage: str
    requested_at: int
    cooling_off_ends_at: int
    purge_due_at: int | None
    purged_at: int | None
    cancellable: bool
    legal_hold_active: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "deletion_request_id": self.deletion_request_id,
            "stage": self.stage,
            "requested_at": self.requested_at,
            "cooling_off_ends_at": self.cooling_off_ends_at,
            "purge_due_at": self.purge_due_at,
            "purged_at": self.purged_at,
            "cancellable": self.cancellable,
            "legal_hold_active": self.legal_hold_active,
        }


def deletion_timeline(
    row: Mapping[str, Any],
    *,
    now: int,
    legal_hold_active: bool,
) -> DeletionTimeline:
    """Project one deletion request into the stage the caller is allowed to see."""
    stage = str(row["stage"])
    moment = int(now)
    cooling_off_ends_at = int(row["cooling_off_ends_at"])
    cancellable = stage == _STAGE_COOLING_OFF and moment < cooling_off_ends_at
    purge_due_at = row.get("purge_due_at")
    purged_at = row.get("purged_at")
    return DeletionTimeline(
        deletion_request_id=str(row["deletion_request_id"]),
        stage=stage,
        requested_at=int(row["requested_at"]),
        cooling_off_ends_at=cooling_off_ends_at,
        purge_due_at=None if purge_due_at is None else int(purge_due_at),
        purged_at=None if purged_at is None else int(purged_at),
        cancellable=cancellable,
        legal_hold_active=bool(legal_hold_active),
    )


# ── purge coverage ──────────────────────────────────────────────────────────

# Tables that hold lifecycle evidence rather than tenant business data.  A purge
# never touches them, and the restore replay counts on them surviving.
PURGE_EVIDENCE_TABLES = frozenset(
    {
        "workbuddy_deletion_ledger",
        "workbuddy_deletion_requests",
        "workbuddy_legal_holds",
        "workbuddy_tenant_archives",
        "workbuddy_tenant_audit_events",
        "workbuddy_tenant_tombstones",
        "workbuddy_tenants",
    }
)


def purge_protects(table_name: str) -> bool:
    return table_name.strip().lower() in PURGE_EVIDENCE_TABLES


@dataclass(frozen=True)
class PurgePlan:
    """How one tenant's non-evidence rows are emptied.

    ``order`` is safe one table at a time (children before the rows they
    reference).  ``batched`` holds the tables that sit on a foreign-key cycle:
    no single-table order can empty them, because whichever table went first
    would still be referenced by the other.  They are therefore emptied together
    in one statement, where PostgreSQL checks every immediate constraint only
    once the statement as a whole is done.
    """

    order: tuple[str, ...]
    batched: tuple[str, ...]

    @property
    def tables(self) -> tuple[str, ...]:
        """Every table this plan touches, in deletion order."""
        return self.order + self.batched


def plan_tenant_purge(
    tables: Iterable[str],
    dependencies: Mapping[str, Iterable[str]],
) -> PurgePlan:
    """Split tenant tables into a deletion order plus whatever is left cyclic.

    ``dependencies`` maps a table to the tables its foreign keys reference (the
    shape returned by ``WorkBuddyLifecycleRepo.table_dependencies``).  A table
    that no remaining table references is a leaf and is deleted first.  A table
    that references itself is a leaf as well: a single ``DELETE`` empties it and
    the self-reference goes away with the rows.  Anything still unpeelable is
    returned as one ``batched`` group.
    """
    remaining = {str(name) for name in tables}
    order: list[str] = []
    while remaining:
        referenced = {
            str(parent)
            for name in remaining
            for parent in dependencies.get(name, ())
            if str(parent) in remaining and str(parent) != name
        }
        leaves = sorted(name for name in remaining if name not in referenced)
        if not leaves:
            break
        order.extend(leaves)
        remaining.difference_update(leaves)
    return PurgePlan(order=tuple(order), batched=tuple(sorted(remaining)))


def order_purge_tables(
    tables: Iterable[str],
    dependencies: Mapping[str, Iterable[str]],
) -> list[str]:
    """Strict deletion order: a foreign-key cycle is refused, never skipped.

    This is the ordering-only contract.  Callers that can empty a cycle in a
    single statement use :func:`plan_tenant_purge` instead; callers that cannot
    must treat a cycle as a defect, because silently dropping it would leave
    tenant rows behind.
    """
    plan = plan_tenant_purge(tables, dependencies)
    if plan.batched:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "tenant purge is blocked by a cyclic table dependency",
            details={"tables": list(plan.batched)},
        )
    return list(plan.order)


# ── anonymized usage linkage ────────────────────────────────────────────────

SUBJECT_KINDS = frozenset({"user", "agent", "connector", "workflow"})
_SALT_BYTES = 32


def new_usage_salt() -> tuple[str, bytes]:
    """Return ``(salt_ref, salt_secret)`` for a tenant's usage hashing."""
    ref = secrets.token_hex(8)
    return ref, secrets.token_bytes(_SALT_BYTES)


def usage_subject_sha256(salt_secret: bytes, subject_kind: str, subject_id: str) -> str:
    """Keyed hash that links usage events to a subject only while the salt lives."""
    if subject_kind not in SUBJECT_KINDS:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "unknown usage subject kind",
            details={"subject_kind": str(subject_kind)},
        )
    body = f"{subject_kind}:{str(subject_id).strip()}".encode()
    return hmac.new(salt_secret, body, hashlib.sha256).hexdigest()


def verify_usage_subject(subject_sha256: str) -> bool:
    """A linkage hash is a 64 character lowercase hex digest."""
    return bool(re.fullmatch(r"[0-9a-f]{64}", subject_sha256.strip()))


# ── orchestration ───────────────────────────────────────────────────────────
#
# Everything below composes repo primitives inside one WorkBuddy transaction per
# unit of work.  The repo never learns the redaction rules and this module never
# writes SQL.

REDEEMED_EXPORT_STATUS = "ready"
_FETCH_LIMIT = MAX_EXPORT_ROWS_PER_TABLE


class ExportTooLargeError(OctopError):
    """An export or archive would exceed the per-table ceiling that keeps it bounded."""

    def __init__(self, table: str, limit: int) -> None:
        super().__init__(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"export of table {table} exceeds the {limit} row export ceiling",
            details={"table": table, "limit": limit},
        )


@dataclass(frozen=True)
class ExportView:
    """Everything the API may show about an export job (never payload contents)."""

    export_job_id: str
    tenant_id: str
    status: str
    requested_by: int | None
    created_at: int
    completed_at: int | None
    redeem_expires_at: int
    redeemed_at: int | None
    failure_reason: str | None
    table_total: int
    row_total: int
    manifest: dict[str, Any] | None
    manifest_sha256: str | None
    content_sha256: str | None
    version: int

    def as_payload(self, *, include_manifest: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "export_job_id": self.export_job_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "redeem_expires_at": self.redeem_expires_at,
            "redeemed_at": self.redeemed_at,
            "table_total": self.table_total,
            "row_total": self.row_total,
            "manifest_sha256": self.manifest_sha256,
            "content_sha256": self.content_sha256,
            "version": self.version,
        }
        if payload["status"] == "failed":
            payload["failure_reason"] = self.failure_reason
        if include_manifest:
            payload["manifest"] = self.manifest
        return payload


@dataclass(frozen=True)
class ExportIssue:
    """Creation result: the job view plus the single raw redeem token."""

    job: ExportView
    redeem_token: str
    redeem_expires_at: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job.export_job_id,
            "export_job_id": self.job.export_job_id,
            "status": self.job.status,
            "redeem_token": self.redeem_token,
            "redeem_expires_at": self.redeem_expires_at,
            "manifest_sha256": self.job.manifest_sha256,
            "table_total": self.job.table_total,
            "row_total": self.job.row_total,
        }


@dataclass(frozen=True)
class ExportDownload:
    """A consumed redeem token: the manifest plus the redacted table payloads."""

    export_job_id: str
    tenant_id: str
    manifest: dict[str, Any]
    manifest_sha256: str
    tables: tuple[dict[str, Any], ...]
    redeemed_at: int


@dataclass(frozen=True)
class DeletionView:
    deletion_request_id: str
    tenant_id: str
    stage: str
    requested_at: int
    cooling_off_ends_at: int
    purge_due_at: int | None
    purged_at: int | None
    cancellable: bool
    legal_hold_active: bool
    version: int
    policy_sha256: str
    policy_expires_at: int
    archive_sha256: str | None
    tombstone_sha256: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "deletion_request_id": self.deletion_request_id,
            "tenant_id": self.tenant_id,
            "stage": self.stage,
            "requested_at": self.requested_at,
            "cooling_off_ends_at": self.cooling_off_ends_at,
            "purge_due_at": self.purge_due_at,
            "purged_at": self.purged_at,
            "cancellable": self.cancellable,
            "legal_hold_active": self.legal_hold_active,
            "version": self.version,
            "policy_sha256": self.policy_sha256,
            "policy_expires_at": self.policy_expires_at,
            "archive_sha256": self.archive_sha256,
            "tombstone_sha256": self.tombstone_sha256,
        }


@dataclass(frozen=True)
class PurgeReport:
    tenant_id: str
    tables_deleted: int
    rows_deleted: int
    salts_destroyed: int
    usage_links_purged: int
    ledger_sequence: int
    ledger_head_sha256: str
    archive_sha256: str | None
    tombstone_sha256: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tables_deleted": self.tables_deleted,
            "rows_deleted": self.rows_deleted,
            "salts_destroyed": self.salts_destroyed,
            "usage_links_purged": self.usage_links_purged,
            "ledger_sequence": self.ledger_sequence,
            "ledger_head_sha256": self.ledger_head_sha256,
            "archive_sha256": self.archive_sha256,
            "tombstone_sha256": self.tombstone_sha256,
        }


@dataclass(frozen=True)
class LifecycleReport:
    archived: tuple[str, ...]
    purged: tuple[str, ...]
    skipped_legal_hold: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "archived": list(self.archived),
            "purged": list(self.purged),
            "skipped_legal_hold": list(self.skipped_legal_hold),
        }


@dataclass(frozen=True)
class ReplayReport:
    tenant_id: str
    action: str
    tombstoned: bool
    reseeded_tables: tuple[str, ...]
    rows_deleted: int
    ledger_sequence: int
    ledger_head_sha256: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "action": self.action,
            "tombstoned": self.tombstoned,
            "reseeded_tables": list(self.reseeded_tables),
            "rows_deleted": self.rows_deleted,
            "ledger_sequence": self.ledger_sequence,
            "ledger_head_sha256": self.ledger_head_sha256,
        }


def _tenant_ctx(tenant_id: str, *, user_id: int | None = None) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id)


def _platform_ctx(
    tenant_id: str | None = None, *, user_id: int | None = None
) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.platform(tenant_id=tenant_id, user_id=user_id)


def _export_view(row: Mapping[str, Any]) -> ExportView:
    manifest: dict[str, Any] | None = None
    if row.get("manifest_json"):
        try:
            loaded = json.loads(str(row["manifest_json"]))
        except ValueError:
            loaded = None
        manifest = loaded if isinstance(loaded, dict) else None
    return ExportView(
        export_job_id=str(row["export_job_id"]),
        tenant_id=str(row["tenant_id"]),
        status=str(row["status"]),
        requested_by=row.get("requested_by"),
        created_at=int(row["created_at"]),
        completed_at=None if row.get("completed_at") is None else int(row["completed_at"]),
        redeem_expires_at=int(row["redeem_expires_at"]),
        redeemed_at=None if row.get("redeemed_at") is None else int(row["redeemed_at"]),
        failure_reason=row.get("failure_reason"),
        table_total=int(row.get("table_total") or 0),
        row_total=int(row.get("row_total") or 0),
        manifest=manifest,
        manifest_sha256=row.get("manifest_sha256"),
        content_sha256=row.get("content_sha256"),
        version=int(row.get("version") or 1),
    )


def tombstone_from_row(row: Mapping[str, Any]) -> Tombstone:
    """Rebuild the tombstone record, including its evidence table list."""
    try:
        evidence = tuple(
            str(name) for name in json.loads(str(row.get("retained_evidence") or "[]"))
        )
    except ValueError:
        evidence = ()
    return Tombstone(
        tenant_id=str(row["tenant_id"]),
        deletion_request_id=str(row["deletion_request_id"]),
        purged_at=int(row["purged_at"]),
        policy_sha256=str(row["policy_sha256"]),
        ledger_head_sha256=str(row["ledger_head_sha256"]),
        ledger_entry_count=int(row["ledger_entry_count"]),
        archive_sha256=row.get("archive_sha256"),
        archive_row_total=None
        if row.get("archive_row_total") is None
        else int(row["archive_row_total"]),
        usage_linkage_sha256=row.get("usage_linkage_sha256"),
        purged_tables=int(row.get("purged_tables") or 0),
        purged_rows=int(row.get("purged_rows") or 0),
        evidence_tables=evidence,
        tombstone_sha256=str(row["tombstone_sha256"]),
    )


def _ledger_entry(
    repo: Any,
    conn: Any,
    *,
    tenant_id: str,
    entry_type: str,
    payload: Mapping[str, Any] | None,
    deletion_request_id: str | None,
    actor_user_id: int | None,
    actor_label: str | None,
    created_at: int,
) -> LedgerEntry:
    """Append one ledger entry, extending the per-tenant hash chain."""
    if entry_type not in LEDGER_ENTRY_TYPES:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "unknown deletion ledger entry type",
            details={"entry_type": str(entry_type)},
        )
    head = repo.ledger_head(conn, tenant_id=tenant_id)
    sequence = 1 if head is None else int(head["sequence"]) + 1
    previous = None if head is None else str(head["entry_sha256"])
    payload_json = canonical_json(dict(payload or {}))
    payload_sha256 = sha256_text(payload_json)
    entry_sha256 = ledger_entry_sha256(
        tenant_id=tenant_id,
        sequence=sequence,
        entry_type=entry_type,
        payload_sha256=payload_sha256,
        previous_sha256=previous,
        created_at=created_at,
    )
    repo.insert_ledger_entry(
        conn,
        tenant_id=tenant_id,
        deletion_request_id=deletion_request_id,
        sequence=sequence,
        entry_type=entry_type,
        payload_json=payload_json,
        payload_sha256=payload_sha256,
        previous_sha256=previous,
        entry_sha256=entry_sha256,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        created_at=created_at,
    )
    return LedgerEntry(
        ledger_entry_id="",
        tenant_id=tenant_id,
        deletion_request_id=deletion_request_id,
        sequence=sequence,
        entry_type=entry_type,
        payload=dict(payload or {}),
        payload_sha256=payload_sha256,
        previous_sha256=previous,
        entry_sha256=entry_sha256,
        created_at=created_at,
    )


def _collect_redacted_tables(
    repo: Any,
    conn: Any,
) -> tuple[list[ExportTable], list[ExportTableExclusion], list[tuple[str, str]]]:
    """Read every tenant table, redact it, and return digests plus payloads."""
    tables = sorted(repo.tenant_tables(conn))
    exported: list[ExportTable] = []
    excluded: list[ExportTableExclusion] = []
    payloads: list[tuple[str, str]] = []
    for table in tables:
        category = excluded_table_category(table)
        if category is not None:
            excluded.append(ExportTableExclusion(name=table, category=category))
            continue
        columns = repo.table_columns(conn, table)
        kept, dropped = exportable_columns(columns)
        if not kept:
            excluded.append(ExportTableExclusion(name=table, category="no_exportable_columns"))
            continue
        rows = repo.fetch_tenant_rows(conn, table=table, columns=kept, limit=_FETCH_LIMIT)
        if len(rows) > _FETCH_LIMIT:
            raise ExportTooLargeError(table, _FETCH_LIMIT)
        redacted = [redact_row(row) for row in rows]
        payload_json = canonical_json(redacted)
        content_sha256 = sha256_text(payload_json)
        exported.append(
            ExportTable(
                name=table,
                row_count=len(redacted),
                columns=kept,
                excluded_columns=dropped,
                content_sha256=content_sha256,
            )
        )
        payloads.append((table, payload_json))
    return exported, excluded, payloads


def start_tenant_export(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int,
    now: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExportIssue:
    """Create, build and freeze one tenant export inside its 72h redeem window."""
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    with repo.transaction(ctx) as conn:
        job = repo.insert_export_job(
            conn,
            tenant_id=tenant_id,
            requested_by=user_id,
            redeem_expires_at=redeem_window_expires_at(moment),
            created_at=moment,
            redaction_rules_version=REDACTION_RULES_VERSION,
        )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="export_requested",
            payload={"export_job_id": str(job["export_job_id"]), "requested_by": user_id},
            deletion_request_id=None,
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
    try:
        with repo.transaction(ctx) as conn:
            exported, excluded, payloads = _collect_redacted_tables(repo, conn)
            manifest = build_export_manifest(
                tenant_id=tenant_id,
                export_job_id=str(job["export_job_id"]),
                created_at=moment,
                tables=exported,
                excluded_tables=excluded,
            )
            digest = manifest_sha256(manifest)
            for table, payload_json in payloads:
                entry = next(item for item in exported if item.name == table)
                repo.insert_export_artifact(
                    conn,
                    tenant_id=tenant_id,
                    export_job_id=str(job["export_job_id"]),
                    table_name=table,
                    row_count=entry.row_count,
                    content_sha256=entry.content_sha256,
                    payload_json=payload_json,
                    created_at=moment,
                )
            ready = repo.update_export_job_ready(
                conn,
                tenant_id=tenant_id,
                export_job_id=str(job["export_job_id"]),
                expected_version=int(job["version"]),
                manifest_json=canonical_json(manifest),
                manifest_sha256=digest,
                content_sha256=str(manifest["content_sha256"]),
                table_total=len(exported),
                row_total=int(manifest["totals"]["rows"]),
                updated_at=moment,
            )
            if not ready:
                raise OctopError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "export job state changed while it was being built",
                )
    except ExportTooLargeError:
        _fail_export(
            repo,
            ctx,
            tenant_id=tenant_id,
            export_job_id=str(job["export_job_id"]),
            reason="row_ceiling_exceeded",
            now=moment,
        )
        raise
    except Exception:
        _fail_export(
            repo,
            ctx,
            tenant_id=tenant_id,
            export_job_id=str(job["export_job_id"]),
            reason="export_build_failed",
            now=moment,
        )
        raise
    token = issue_redeem_token()
    with repo.transaction(ctx) as conn:
        repo.revoke_live_redeem_tokens(
            conn, tenant_id=tenant_id, export_job_id=str(job["export_job_id"]), revoked_at=moment
        )
        record = repo.insert_redeem_token(
            conn,
            tenant_id=tenant_id,
            export_job_id=str(job["export_job_id"]),
            token_sha256=token.token_sha256,
            issued_by=user_id,
            issued_at=moment,
            expires_at=int(job["redeem_expires_at"]),
        )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="export_redeem_issued",
            payload={
                "export_job_id": str(job["export_job_id"]),
                "redeem_token_id": str(record["redeem_token_id"]),
                "expires_at": int(record["expires_at"]),
            },
            deletion_request_id=None,
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
    return ExportIssue(
        job=_export_view(
            {
                **job,
                "status": "ready",
                "manifest_json": canonical_json(manifest),
                "manifest_sha256": digest,
                "content_sha256": manifest["content_sha256"],
                "table_total": len(exported),
                "row_total": manifest["totals"]["rows"],
                "completed_at": moment,
                "redeemed_at": None,
                "failure_reason": None,
            }
        ),
        redeem_token=token.raw,
        redeem_expires_at=int(job["redeem_expires_at"]),
    )


def _fail_export(
    repo: Any,
    ctx: WorkBuddyDbContext,
    *,
    tenant_id: str,
    export_job_id: str,
    reason: str,
    now: int,
) -> None:
    with repo.transaction(ctx) as conn:
        repo.update_export_job_failed(
            conn,
            tenant_id=tenant_id,
            export_job_id=export_job_id,
            failure_reason=reason,
            updated_at=now,
        )


def read_export_job(repo: Any, *, tenant_id: str, export_job_id: str) -> ExportView | None:
    ctx = _tenant_ctx(tenant_id)
    with repo.transaction(ctx) as conn:
        row = repo.get_export_job(conn, tenant_id=tenant_id, export_job_id=export_job_id)
    return None if row is None else _export_view(row)


def read_export_manifest(repo: Any, *, tenant_id: str, export_job_id: str) -> dict[str, Any] | None:
    """Manifest + digest for a job that finished building (payload stays sealed)."""
    view = read_export_job(repo, tenant_id=tenant_id, export_job_id=export_job_id)
    if view is None:
        return None
    if view.manifest is None or view.manifest_sha256 is None:
        raise OctopError(
            ErrorCode.EXPORT_JOB_NOT_FOUND,
            "export manifest is not available for this job",
        )
    return {
        "export_job_id": view.export_job_id,
        "status": view.status,
        "manifest_sha256": view.manifest_sha256,
        "content_sha256": view.content_sha256,
        "manifest": view.manifest,
    }


def redeem_export(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int,
    token: str,
    now: int | None = None,
) -> ExportDownload:
    """Consume a redeem token exactly once and hand back the verified payload."""
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    digest = hash_redeem_token(token)
    with repo.transaction(ctx) as conn:
        stored = repo.get_redeem_token(conn, tenant_id=tenant_id, token_sha256=digest)
        failure = redeem_token_failure(stored, now=moment)
        if failure is not None:
            raise redeem_token_error(failure)
        if not repo.consume_redeem_token(
            conn, tenant_id=tenant_id, token_sha256=digest, consumed_by=user_id, consumed_at=moment
        ):
            raise redeem_token_error(ErrorCode.EXPORT_REDEEM_CONSUMED)
        export_job_id = str(stored["export_job_id"])
        job = repo.get_export_job(conn, tenant_id=tenant_id, export_job_id=export_job_id)
        if job is None or not job.get("manifest_json") or not job.get("manifest_sha256"):
            raise OctopError(
                ErrorCode.EXPORT_REDEEM_INVALID,
                "export job is not ready to redeem",
            )
        manifest = json.loads(str(job["manifest_json"]))
        artifacts = repo.list_export_artifacts(
            conn, tenant_id=tenant_id, export_job_id=export_job_id
        )
        entries = {str(entry["name"]): entry for entry in manifest.get("tables", [])}
        tables: list[dict[str, Any]] = []
        for artifact in artifacts:
            name = str(artifact["table_name"])
            expected = entries.get(name)
            if expected is None or expected.get("content_sha256") != artifact["content_sha256"]:
                raise OctopError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "export payload does not match its manifest",
                )
            if sha256_text(str(artifact["payload_json"])) != artifact["content_sha256"]:
                raise OctopError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "export payload failed its integrity check",
                )
            tables.append(
                {
                    "name": name,
                    "row_count": int(artifact["row_count"]),
                    "content_sha256": str(artifact["content_sha256"]),
                    "rows": json.loads(str(artifact["payload_json"])),
                }
            )
        repo.mark_export_redeemed(
            conn,
            tenant_id=tenant_id,
            export_job_id=export_job_id,
            redeemed_by=user_id,
            redeemed_at=moment,
        )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="export_redeemed",
            payload={"export_job_id": export_job_id, "redeemed_by": user_id},
            deletion_request_id=None,
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
    return ExportDownload(
        export_job_id=export_job_id,
        tenant_id=tenant_id,
        manifest=manifest,
        manifest_sha256=str(job["manifest_sha256"]),
        tables=tuple(tables),
        redeemed_at=moment,
    )


def expire_exports(repo: Any, *, tenant_id: str | None = None, now: int | None = None) -> int:
    """Drop export payloads past the 72h window; the manifest stays as evidence."""
    moment = int(now if now is not None else time.time())
    with repo.transaction(_platform_ctx(tenant_id)) as conn:
        jobs = repo.expire_export_jobs(conn, now=moment, tenant_id=tenant_id)
        for job in jobs:
            _ledger_entry(
                repo,
                conn,
                tenant_id=str(job["tenant_id"]),
                entry_type="export_expired",
                payload={"export_job_id": str(job["export_job_id"])},
                deletion_request_id=None,
                actor_user_id=None,
                actor_label="lifecycle-maintenance",
                created_at=moment,
            )
    return len(jobs)


# ── deletion requests ───────────────────────────────────────────────────────


def _deletion_row(
    repo: Any, conn: Any, *, tenant_id: str, deletion_request_id: str
) -> dict[str, Any]:
    row: dict[str, Any] | None = repo.get_deletion_request(
        conn, tenant_id=tenant_id, deletion_request_id=deletion_request_id
    )
    if row is None:
        raise OctopError(
            ErrorCode.NOT_FOUND,
            "deletion request not found",
            details={"deletion_request_id": str(deletion_request_id)},
        )
    return row


def _deletion_view(repo: Any, conn: Any, row: Mapping[str, Any], *, now: int) -> DeletionView:
    hold = repo.count_active_legal_holds(
        conn,
        tenant_id=str(row["tenant_id"]),
        deletion_request_id=str(row["deletion_request_id"]),
    )
    timeline = deletion_timeline(row, now=now, legal_hold_active=bool(hold))
    tombstone = repo.get_tombstone(conn, tenant_id=str(row["tenant_id"]))
    return DeletionView(
        deletion_request_id=timeline.deletion_request_id,
        tenant_id=str(row["tenant_id"]),
        stage=timeline.stage,
        requested_at=timeline.requested_at,
        cooling_off_ends_at=timeline.cooling_off_ends_at,
        purge_due_at=timeline.purge_due_at,
        purged_at=timeline.purged_at,
        cancellable=timeline.cancellable,
        legal_hold_active=timeline.legal_hold_active,
        version=int(row["version"]),
        policy_sha256=str(row["policy_sha256"]),
        policy_expires_at=int(row["policy_expires_at"]),
        archive_sha256=row.get("archive_sha256"),
        tombstone_sha256=None if tombstone is None else str(tombstone["tombstone_sha256"]),
    )


def request_tenant_deletion(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int,
    environ: Mapping[str, str] | None = None,
    now: int | None = None,
) -> DeletionView:
    """Open a 30-day cooling-off deletion request behind the signed policy gate."""
    moment = int(now if now is not None else time.time())
    policy = require_compliance_policy(tenant_id, environ=environ, now=moment)
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    with repo.transaction(ctx) as conn:
        if not repo.tenant_exists(conn, tenant_id=tenant_id):
            raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
        active = repo.find_active_deletion_request(conn, tenant_id=tenant_id)
        if active is not None:
            raise OctopError(
                ErrorCode.DELETION_REQUEST_CONFLICT,
                "a deletion request is already open for this tenant",
                details={"deletion_request_id": str(active["deletion_request_id"])},
            )
        row = repo.insert_deletion_request(
            conn,
            tenant_id=tenant_id,
            requested_by=user_id,
            requested_at=moment,
            cooling_off_ends_at=moment + COOLING_OFF_SECONDS,
            policy_sha256=policy.digest,
            policy_expires_at=policy.expires_at,
        )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="deletion_requested",
            payload={
                "deletion_request_id": str(row["deletion_request_id"]),
                **policy.as_ledger_payload(),
            },
            deletion_request_id=str(row["deletion_request_id"]),
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
        return _deletion_view(repo, conn, row, now=moment)


def read_deletion_request(
    repo: Any, *, tenant_id: str, deletion_request_id: str, now: int | None = None
) -> DeletionView:
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id)
    with repo.transaction(ctx) as conn:
        row = _deletion_row(
            repo, conn, tenant_id=tenant_id, deletion_request_id=deletion_request_id
        )
        return _deletion_view(repo, conn, row, now=moment)


def cancel_tenant_deletion(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int,
    deletion_request_id: str,
    expected_version: int | None = None,
    now: int | None = None,
) -> DeletionView:
    """Cancel inside the cooling-off window with a version CAS, never afterwards."""
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    with repo.transaction(ctx) as conn:
        row = _deletion_row(
            repo, conn, tenant_id=tenant_id, deletion_request_id=deletion_request_id
        )
        if str(row["stage"]) != "cooling_off" or moment >= int(row["cooling_off_ends_at"]):
            raise OctopError(
                ErrorCode.DELETION_CANCEL_WINDOW_CLOSED,
                "the cooling-off window for this deletion request has closed",
                details={"deletion_request_id": str(deletion_request_id)},
            )
        version = int(row["version"]) if expected_version is None else int(expected_version)
        if not repo.cancel_deletion_request(
            conn,
            tenant_id=tenant_id,
            deletion_request_id=deletion_request_id,
            expected_version=version,
            cancelled_by=user_id,
            cancelled_at=moment,
        ):
            raise OctopError(
                ErrorCode.DELETION_REQUEST_CONFLICT,
                "the deletion request changed before it could be cancelled",
                details={"deletion_request_id": str(deletion_request_id)},
            )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="deletion_cancelled",
            payload={"deletion_request_id": str(deletion_request_id), "cancelled_by": user_id},
            deletion_request_id=str(deletion_request_id),
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
        updated = _deletion_row(
            repo, conn, tenant_id=tenant_id, deletion_request_id=deletion_request_id
        )
        return _deletion_view(repo, conn, updated, now=moment)


def place_tenant_legal_hold(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int | None,
    matter_reference: str,
    reason: str,
    deletion_request_id: str | None = None,
    actor_label: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    with repo.transaction(ctx) as conn:
        hold = repo.insert_legal_hold(
            conn,
            tenant_id=tenant_id,
            deletion_request_id=deletion_request_id,
            matter_reference=matter_reference,
            reason=reason,
            placed_by=user_id,
            placed_by_label=actor_label,
            placed_at=moment,
        )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="legal_hold_placed",
            payload={
                "legal_hold_id": str(hold["legal_hold_id"]),
                "matter_reference": matter_reference,
                "deletion_request_id": deletion_request_id,
            },
            deletion_request_id=deletion_request_id,
            actor_user_id=user_id,
            actor_label=actor_label,
            created_at=moment,
        )
        return dict(hold)


def release_tenant_legal_hold(
    repo: Any,
    *,
    tenant_id: str,
    user_id: int | None,
    legal_hold_id: str,
    release_reason: str,
    now: int | None = None,
) -> dict[str, Any]:
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id, user_id=user_id)
    with repo.transaction(ctx) as conn:
        released = repo.release_legal_hold(
            conn,
            tenant_id=tenant_id,
            legal_hold_id=legal_hold_id,
            released_by=user_id,
            release_reason=release_reason,
            released_at=moment,
        )
        if not released:
            raise OctopError(
                ErrorCode.NOT_FOUND,
                "legal hold not found",
                details={"legal_hold_id": str(legal_hold_id)},
            )
        _ledger_entry(
            repo,
            conn,
            tenant_id=tenant_id,
            entry_type="legal_hold_released",
            payload={"legal_hold_id": str(legal_hold_id), "release_reason": release_reason},
            deletion_request_id=None,
            actor_user_id=user_id,
            actor_label=None,
            created_at=moment,
        )
        return {"legal_hold_id": str(legal_hold_id), "released_at": moment}


# ── archive, purge and restore replay ───────────────────────────────────────


def _active_hold(conn: Any, repo: Any, row: Mapping[str, Any]) -> bool:
    holds: int = repo.count_active_legal_holds(
        conn,
        tenant_id=str(row["tenant_id"]),
        deletion_request_id=str(row["deletion_request_id"]),
    )
    return holds > 0


def _purge_table_order(repo: Any, conn: Any, *, tenant_id: str) -> tuple[PurgePlan, dict[str, int]]:
    """Purge plan over the evidence-free tenant tables plus pre-delete row counts."""
    tables = [table for table in repo.tenant_tables(conn) if not purge_protects(table)]
    plan = plan_tenant_purge(tables, repo.table_dependencies(conn))
    before = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=plan.tables)
    return plan, before


def _archive_tenant(repo: Any, conn: Any, *, row: Mapping[str, Any], now: int) -> dict[str, Any]:
    """Freeze the tenant snapshot (same redaction rules as an export) for retention."""
    tenant_id = str(row["tenant_id"])
    request_id = str(row["deletion_request_id"])
    exported, excluded, payloads = _collect_redacted_tables(repo, conn)
    archive: dict[str, Any] = {
        "schema": EXPORT_SCHEMA,
        "redaction_rules_version": REDACTION_RULES_VERSION,
        "tenant_id": tenant_id,
        "deletion_request_id": request_id,
        "created_at": now,
        "tables": [entry.as_manifest_entry() for entry in exported],
        "excluded_tables": [entry.as_manifest_entry() for entry in excluded],
        "totals": {
            "tables": len(exported),
            "rows": sum(entry.row_count for entry in exported),
        },
    }
    payload_json = canonical_json(
        {"manifest": archive, "tables": {name: json.loads(body) for name, body in payloads}}
    )
    archive_sha256 = sha256_text(payload_json)
    repo.insert_archive(
        conn,
        tenant_id=tenant_id,
        deletion_request_id=request_id,
        archive_sha256=archive_sha256,
        table_total=len(exported),
        row_total=int(archive["totals"]["rows"]),
        payload_json=payload_json,
        retained_until=now + ARCHIVE_RETENTION_SECONDS,
        created_at=now,
    )
    if not repo.mark_deletion_archived(
        conn,
        tenant_id=tenant_id,
        deletion_request_id=request_id,
        expected_version=int(row["version"]),
        archived_at=now,
        archive_sha256=archive_sha256,
        archive_row_total=int(archive["totals"]["rows"]),
        purge_due_at=now + ARCHIVE_RETENTION_SECONDS,
    ):
        raise OctopError(
            ErrorCode.DELETION_REQUEST_CONFLICT,
            "the deletion request changed while it was being archived",
        )
    _ledger_entry(
        repo,
        conn,
        tenant_id=tenant_id,
        entry_type="archive_created",
        payload={
            "deletion_request_id": request_id,
            "archive_sha256": archive_sha256,
            "row_total": int(archive["totals"]["rows"]),
            "retained_until": now + ARCHIVE_RETENTION_SECONDS,
        },
        deletion_request_id=request_id,
        actor_user_id=None,
        actor_label="lifecycle-maintenance",
        created_at=now,
    )
    return {"archive_sha256": archive_sha256, "row_total": int(archive["totals"]["rows"])}


def _purge_tenant(repo: Any, conn: Any, *, row: Mapping[str, Any], now: int) -> PurgeReport:
    """Delete tenant data, destroy linkage salts, chain the ledger and tombstone it."""
    tenant_id = str(row["tenant_id"])
    request_id = str(row["deletion_request_id"])
    if _active_hold(conn, repo, row):
        raise OctopError(
            ErrorCode.LEGAL_HOLD_ACTIVE,
            "an active legal hold blocks the purge",
            details={"deletion_request_id": request_id},
        )
    plan, before = _purge_table_order(repo, conn, tenant_id=tenant_id)
    deleted = repo.delete_tenant_rows(conn, tenant_id=tenant_id, tables=plan.order)
    if plan.batched:
        deleted.update(
            repo.delete_tenant_rows_batched(conn, tenant_id=tenant_id, tables=plan.batched)
        )
    salts_destroyed = repo.destroy_usage_salts(conn, tenant_id=tenant_id, destroyed_at=now)
    links_purged = repo.mark_usage_links_purged(conn, tenant_id=tenant_id, purged_at=now)
    linkage = repo.usage_linkage_digest_input(conn, tenant_id=tenant_id)
    usage_linkage_sha256 = sha256_json(linkage)
    _ledger_entry(
        repo,
        conn,
        tenant_id=tenant_id,
        entry_type="usage_linkage_anonymized",
        payload={
            "deletion_request_id": request_id,
            "salts_destroyed": salts_destroyed,
            "usage_links_purged": links_purged,
            "usage_linkage_sha256": usage_linkage_sha256,
        },
        deletion_request_id=request_id,
        actor_user_id=None,
        actor_label="lifecycle-maintenance",
        created_at=now,
    )
    rows_deleted = sum(deleted.values())
    purged_tables = sum(1 for count in deleted.values() if count > 0)
    _ledger_entry(
        repo,
        conn,
        tenant_id=tenant_id,
        entry_type="purge_completed",
        payload={
            "deletion_request_id": request_id,
            "tables_deleted": purged_tables,
            "rows_deleted": rows_deleted,
            "rows_before": sum(before.values()),
            "policy_sha256": str(row["policy_sha256"]),
        },
        deletion_request_id=request_id,
        actor_user_id=None,
        actor_label="lifecycle-maintenance",
        created_at=now,
    )
    head = repo.ledger_head(conn, tenant_id=tenant_id)
    entry_count = repo.count_ledger_entries(conn, tenant_id=tenant_id)
    if head is None:
        raise OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, "deletion ledger is not writable")
    evidence = canonical_json(sorted(PURGE_EVIDENCE_TABLES))
    archive_sha256 = row.get("archive_sha256")
    digest = tombstone_digest(
        tenant_id=tenant_id,
        deletion_request_id=request_id,
        purged_at=now,
        policy_sha256=str(row["policy_sha256"]),
        ledger_head_sha256=str(head["entry_sha256"]),
        ledger_entry_count=entry_count,
        purged_tables=purged_tables,
        purged_rows=rows_deleted,
    )
    repo.insert_tombstone(
        conn,
        tenant_id=tenant_id,
        deletion_request_id=request_id,
        purged_at=now,
        policy_sha256=str(row["policy_sha256"]),
        ledger_head_sha256=str(head["entry_sha256"]),
        ledger_entry_count=entry_count,
        archive_sha256=None if archive_sha256 is None else str(archive_sha256),
        archive_row_total=None
        if row.get("archive_row_total") is None
        else int(row["archive_row_total"]),
        usage_linkage_sha256=usage_linkage_sha256,
        purged_tables=purged_tables,
        purged_rows=rows_deleted,
        retained_evidence=evidence,
        tombstone_sha256=digest,
        created_at=now,
    )
    repo.clear_archive_payload(
        conn, tenant_id=tenant_id, deletion_request_id=request_id, purged_at=now
    )
    if not repo.mark_deletion_purged(
        conn,
        tenant_id=tenant_id,
        deletion_request_id=request_id,
        expected_version=int(row["version"]),
        purged_at=now,
        ledger_sequence=int(head["sequence"]),
    ):
        raise OctopError(
            ErrorCode.DELETION_REQUEST_CONFLICT,
            "the deletion request changed while it was being purged",
        )
    remaining = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=plan.tables)
    leftovers = {table: count for table, count in remaining.items() if count > 0}
    if leftovers:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "purge left tenant rows behind",
            details={"tables": sorted(leftovers)},
        )
    return PurgeReport(
        tenant_id=tenant_id,
        tables_deleted=purged_tables,
        rows_deleted=rows_deleted,
        salts_destroyed=salts_destroyed,
        usage_links_purged=links_purged,
        ledger_sequence=int(head["sequence"]),
        ledger_head_sha256=str(head["entry_sha256"]),
        archive_sha256=None if archive_sha256 is None else str(archive_sha256),
        tombstone_sha256=digest,
    )


def advance_deletion_lifecycle(repo: Any, *, now: int | None = None) -> LifecycleReport:
    """Advance every due request: cooling-off end archives, retention end purges.

    Legal holds are honoured: a held request is reported and left untouched, and
    every step runs in one platform transaction so a purge is all-or-nothing.
    """
    moment = int(now if now is not None else time.time())
    archived: list[str] = []
    purged: list[str] = []
    held: list[str] = []
    with repo.transaction(_platform_ctx()) as conn:
        for row in repo.due_deletion_requests(conn, now=moment):
            request_id = str(row["deletion_request_id"])
            tenant_id = str(row["tenant_id"])
            if _active_hold(conn, repo, row):
                held.append(request_id)
                continue
            stage = str(row["stage"])
            if stage == "cooling_off":
                _archive_tenant(repo, conn, row=row, now=moment)
                archived.append(request_id)
            elif stage == "archived":
                _purge_tenant(repo, conn, row=row, now=moment)
                purged.append(request_id)
            del tenant_id
    return LifecycleReport(
        archived=tuple(archived), purged=tuple(purged), skipped_legal_hold=tuple(held)
    )


def replay_deletion_ledger(repo: Any, *, tenant_id: str, now: int | None = None) -> ReplayReport:
    """Re-apply a recorded purge after a restore, before the service serves data.

    The ledger and the tombstone — not the restored snapshot — decide what a
    tenant may keep: a tombstoned tenant whose snapshot reintroduced rows is
    purged again, and the replay is recorded on the append-only ledger.
    """
    moment = int(now if now is not None else time.time())
    with repo.transaction(_platform_ctx(tenant_id)) as conn:
        tombstone_row = repo.get_tombstone(conn, tenant_id=tenant_id)
        tombstone = None if tombstone_row is None else tombstone_from_row(tombstone_row)
        if tombstone is not None and not tombstone.is_valid():
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "tenant tombstone failed its integrity check",
                details={"tenant_id": str(tenant_id)},
            )
        tables = [table for table in repo.tenant_tables(conn) if not purge_protects(table)]
        counts = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=tables)
        reseeded = tuple(sorted(table for table, count in counts.items() if count > 0))
        entry_count = repo.count_ledger_entries(conn, tenant_id=tenant_id)
        plan = build_restore_replay_plan(
            tenant_id=tenant_id,
            tombstone=tombstone,
            ledger_entries=entry_count,
            reseeded_tables=reseeded,
        )
        if not plan.must_repurge:
            return ReplayReport(
                tenant_id=str(tenant_id),
                action=plan.action,
                tombstoned=False,
                reseeded_tables=(),
                rows_deleted=0,
                ledger_sequence=0,
                ledger_head_sha256=None,
            )
        purge_plan = plan_tenant_purge(tables, repo.table_dependencies(conn))
        deleted = repo.delete_tenant_rows(conn, tenant_id=tenant_id, tables=purge_plan.order)
        if purge_plan.batched:
            deleted.update(
                repo.delete_tenant_rows_batched(
                    conn, tenant_id=tenant_id, tables=purge_plan.batched
                )
            )
        repo.destroy_usage_salts(conn, tenant_id=tenant_id, destroyed_at=moment)
        repo.mark_usage_links_purged(conn, tenant_id=tenant_id, purged_at=moment)
        rows_deleted = sum(deleted.values())
        entry = _ledger_entry(
            repo,
            conn,
            tenant_id=str(tenant_id),
            entry_type="restore_replayed",
            payload={
                "deletion_request_id": plan.tenant_id,
                "purged_at": plan.purged_at,
                "rows_deleted": rows_deleted,
                "reseeded_tables": list(reseeded),
                "ledger_head_sha256": plan.ledger_head_sha256,
            },
            deletion_request_id=tombstone.deletion_request_id if tombstone else None,
            actor_user_id=None,
            actor_label="restore-replay",
            created_at=moment,
        )
        remaining = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=tables)
        leftovers = {table: count for table, count in remaining.items() if count > 0}
        if leftovers:
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "restore replay left purged tenant rows behind",
                details={"tables": sorted(leftovers)},
            )
    return ReplayReport(
        tenant_id=str(tenant_id),
        action=plan.action,
        tombstoned=True,
        reseeded_tables=reseeded,
        rows_deleted=rows_deleted,
        ledger_sequence=entry.sequence,
        ledger_head_sha256=entry.entry_sha256,
    )


def replay_all_tombstoned(repo: Any, *, now: int | None = None) -> tuple[ReplayReport, ...]:
    """Restore gate: every tombstoned tenant is re-purged before readiness."""
    moment = int(now if now is not None else time.time())
    with repo.transaction(_platform_ctx()) as conn:
        rows = repo.list_tombstones(conn)
    return tuple(
        replay_deletion_ledger(repo, tenant_id=str(row["tenant_id"]), now=moment) for row in rows
    )


def verify_purge_coverage(repo: Any, *, tenant_id: str) -> dict[str, int]:
    """Rows still stored for a tombstoned tenant: an empty mapping is the invariant."""
    with repo.transaction(_platform_ctx(tenant_id)) as conn:
        tables = [table for table in repo.tenant_tables(conn) if not purge_protects(table)]
        counts = repo.tenant_row_counts(conn, tenant_id=tenant_id, tables=tables)
    return {table: count for table, count in counts.items() if count > 0}


# ── anonymized usage linkage ────────────────────────────────────────────────


def record_usage_link(
    repo: Any,
    *,
    tenant_id: str,
    subject_kind: str,
    subject_id: str,
    event_count: int = 1,
    now: int | None = None,
) -> str:
    """Link one usage event to a keyed subject hash (never to a raw subject id)."""
    moment = int(now if now is not None else time.time())
    ctx = _tenant_ctx(tenant_id)
    with repo.transaction(ctx) as conn:
        salt = repo.live_usage_salt(conn, tenant_id=tenant_id)
        if salt is None:
            ref, secret = new_usage_salt()
            repo.insert_usage_salt(
                conn, tenant_id=tenant_id, salt_ref=ref, salt_secret=secret, created_at=moment
            )
            salt_ref, salt_secret = ref, secret
        else:
            salt_ref = str(salt["salt_ref"])
            raw = salt["salt_secret"]
            salt_secret = bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else b""
        digest = usage_subject_sha256(salt_secret, subject_kind, subject_id)
        repo.upsert_usage_link(
            conn,
            tenant_id=tenant_id,
            salt_ref=salt_ref,
            subject_kind=subject_kind,
            subject_sha256=digest,
            seen_at=moment,
            event_count=event_count,
        )
    return digest
