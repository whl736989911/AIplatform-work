-- Schema v31: WorkBuddy re-authentication credentials — the five-minute,
-- one-time, purpose-bound grant that the stage-D routes exchange for a fresh
-- export download challenge.
--
-- PostgreSQL only. SQLite installs apply the no-op watermark
-- 031_workbuddy_reauth_credentials.sql and every WorkBuddy entry point fails
-- closed in octop.infra.db.workbuddy_context.workbuddy_transaction, so SQLite
-- can never act as a fallback for a credential store.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * each top-level statement ends its final line with a semicolon, and no
--     statement may contain a semicolon directly followed by a newline, so this
--     file reuses the guard functions created by 015_workbuddy_identity.pg.sql
--     instead of declaring new plpgsql bodies;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.
--
-- Only the sha256 of a credential is stored, it is bound to one tenant, one
-- user and one purpose, it lives for at most five minutes (the ceiling is a
-- CHECK constraint, mirroring the 72h ceiling on redeem tokens), and it is
-- consumed by a single conditional UPDATE so exactly one caller can win.

CREATE TABLE IF NOT EXISTS workbuddy_reauth_credentials (
  reauth_credential_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose              TEXT NOT NULL,
  credential_sha256    TEXT NOT NULL,
  issued_at            BIGINT NOT NULL,
  expires_at           BIGINT NOT NULL,
  consumed_at          BIGINT,
  CONSTRAINT workbuddy_reauth_credentials_hash_key UNIQUE (credential_sha256),
  CONSTRAINT workbuddy_reauth_credentials_id_key UNIQUE (tenant_id, reauth_credential_id),
  CONSTRAINT workbuddy_reauth_credentials_purpose_check CHECK (char_length(btrim(purpose)) BETWEEN 1 AND 64),
  CONSTRAINT workbuddy_reauth_credentials_ttl_check CHECK (expires_at > issued_at AND expires_at <= issued_at + 300),
  CONSTRAINT workbuddy_reauth_credentials_consumed_check CHECK (consumed_at IS NULL OR consumed_at >= issued_at)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_reauth_credentials_holder
  ON workbuddy_reauth_credentials(tenant_id, user_id, purpose) WHERE consumed_at IS NULL;

DROP TRIGGER IF EXISTS workbuddy_reauth_credentials_immutable_id ON workbuddy_reauth_credentials;
CREATE TRIGGER workbuddy_reauth_credentials_immutable_id BEFORE UPDATE ON workbuddy_reauth_credentials
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('reauth_credential_id');

-- ── Row level security: isolated by app.tenant_id like every tenant table ────

ALTER TABLE workbuddy_reauth_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_reauth_credentials FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_reauth_credentials_tenant_isolation ON workbuddy_reauth_credentials;
CREATE POLICY workbuddy_reauth_credentials_tenant_isolation ON workbuddy_reauth_credentials USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

REVOKE ALL ON TABLE workbuddy_reauth_credentials FROM PUBLIC;

UPDATE _schema_version SET version = 31;
