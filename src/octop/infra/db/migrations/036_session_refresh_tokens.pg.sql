-- Schema v36: session refresh tokens (core control plane, not WorkBuddy).
-- One login opens a token family; rotation stamps ``rotated_at`` and the whole
-- family is revoked once a rotated token shows up again (replay evidence).
-- Only sha256 hashes are stored — a raw token never reaches the database.

CREATE TABLE IF NOT EXISTS refresh_tokens (
  id          TEXT PRIMARY KEY,                 -- random id, not a secret
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL UNIQUE,             -- sha256 only
  family_id   TEXT NOT NULL,                    -- one login = one family
  created_at  BIGINT NOT NULL,                  -- unix seconds
  expires_at  BIGINT NOT NULL,
  rotated_at  BIGINT,                           -- non-null = already rotated
  revoked_at  BIGINT
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user
  ON refresh_tokens(user_id, family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family
  ON refresh_tokens(family_id);

UPDATE _schema_version SET version = 36;
