"""Refresh token table access — rotation, family revocation and replay evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts


@dataclass(frozen=True)
class RefreshTokenRow:
    id: str
    user_id: int
    token_hash: str
    family_id: str
    created_at: int
    expires_at: int
    rotated_at: int | None
    revoked_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> RefreshTokenRow:
        return cls(
            id=str(row["id"]),
            user_id=int(row["user_id"]),
            token_hash=str(row["token_hash"]),
            family_id=str(row["family_id"]),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
            rotated_at=int(row["rotated_at"]) if row["rotated_at"] is not None else None,
            revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        )


class RefreshTokenRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        user_id: int,
        token_hash: str,
        family_id: str,
        expires_at: int,
        token_id: str | None = None,
    ) -> str:
        new_id = token_id or str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO refresh_tokens("
                "id, user_id, token_hash, family_id, created_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (new_id, user_id, token_hash, family_id, now_ts(), expires_at),
            )
        return new_id

    def get_by_hash(self, token_hash: str) -> RefreshTokenRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return RefreshTokenRow.from_row(row) if row else None

    def rotate(self, token_id: str, *, rotated_at: int | None = None) -> bool:
        """Mark one token rotated. False when it was rotated or revoked already.

        The second rotation of the same row is the replay signal the caller
        turns into a family revocation.
        """
        ts = rotated_at if rotated_at is not None else now_ts()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_tokens SET rotated_at = ? "
                "WHERE id = ? AND rotated_at IS NULL AND revoked_at IS NULL",
                (ts, token_id),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def revoke_family(self, family_id: str, *, revoked_at: int | None = None) -> int:
        """Revoke every live token in a family; returns how many rows moved."""
        ts = revoked_at if revoked_at is not None else now_ts()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? "
                "WHERE family_id = ? AND revoked_at IS NULL",
                (ts, family_id),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def revoke(self, token_id: str, *, revoked_at: int | None = None) -> bool:
        ts = revoked_at if revoked_at is not None else now_ts()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (ts, token_id),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def purge_expired(self, *, now: int | None = None) -> int:
        """Delete expired rows; returns the number of tokens dropped."""
        ts = now if now is not None else now_ts()
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (ts,))
        return int(getattr(cursor, "rowcount", 0) or 0)
