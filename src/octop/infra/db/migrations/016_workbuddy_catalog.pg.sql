-- Schema v16: WorkBuddy connector-credential governance and immutable platform catalog.
--
-- PostgreSQL only. SQLite installs apply the no-op marker
-- 016_workbuddy_catalog.sql, and octop.infra.db.workbuddy_context refuses to
-- serve WorkBuddy with a controlled WORKBUDDY_POSTGRES_REQUIRED error. Requires
-- PostgreSQL 13+ (gen_random_uuid is core since 13).
--
-- Design notes:
--   * Raw credential material never lands in these tables. ``external_ref`` is a
--     server-generated pointer (scheme://path) into the deployment secret store,
--     and the CHECK below rejects anything that is not a pointer, so a pasted
--     secret cannot be persisted by accident.
--   * Credential revisions are an append-only ledger guarded by the shared
--     workbuddy_guard_append_only trigger from 015; a credential row keeps its
--     identity, steps exactly one revision per write, and revocation is terminal.
--   * Platform tool and model rows are fixed revisions identified only by
--     adapter_key plus tool_key/model_key plus display metadata. Deletes and
--     descriptor edits are rejected; only a one-way revocation may pass.
--   * Tenant tables repeat tenant_id so composite foreign keys make a
--     cross-tenant reference unrepresentable, and use ENABLE + FORCE ROW LEVEL
--     SECURITY with workbuddy_rls_visible from 015. Platform catalog tables are
--     global: readable by every tenant context, writable only under system
--     context.
--   * Capability grants may only reference published revisions, and a tenant
--     default model must itself be a published revision.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on the regex "; \s* \n", so each top-level statement
--     must end its line with a semicolon, and a plpgsql body must never contain a
--     semicolon directly followed by a newline: end such a line with a trailing
--     comment ("; -- ...") so the split cannot land inside the function body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- ── Connector credential metadata ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_connector_credentials (
  credential_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  name                     TEXT NOT NULL,
  connector_kind           TEXT NOT NULL,
  description              TEXT NOT NULL DEFAULT '',
  scopes                   TEXT NOT NULL DEFAULT '[]',
  status                   TEXT NOT NULL DEFAULT 'active',
  revision                 INTEGER NOT NULL DEFAULT 1,
  external_ref             TEXT NOT NULL,
  owner_membership_id      UUID NOT NULL,
  created_at               BIGINT NOT NULL,
  updated_at               BIGINT NOT NULL,
  revoked_at               BIGINT,
  revoked_by_membership_id UUID,
  CONSTRAINT workbuddy_connector_credentials_tenant_id_key UNIQUE (tenant_id, credential_id),
  CONSTRAINT workbuddy_connector_credentials_tenant_name_key UNIQUE (tenant_id, name),
  CONSTRAINT workbuddy_connector_credentials_status_check CHECK (status IN ('active', 'revoked')),
  CONSTRAINT workbuddy_connector_credentials_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT workbuddy_connector_credentials_kind_check CHECK (char_length(btrim(connector_kind)) BETWEEN 1 AND 120),
  CONSTRAINT workbuddy_connector_credentials_description_check CHECK (char_length(description) <= 1000),
  CONSTRAINT workbuddy_connector_credentials_scopes_check CHECK (jsonb_typeof(scopes::jsonb) = 'array'),
  CONSTRAINT workbuddy_connector_credentials_external_ref_check CHECK (
    external_ref ~ '^[a-z][a-z0-9+.-]*://' AND char_length(external_ref) <= 512
  ),
  CONSTRAINT workbuddy_connector_credentials_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_connector_credentials_revocation_check CHECK (
    (status = 'revoked') = (revoked_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_credentials_tenant_status
  ON workbuddy_connector_credentials(tenant_id, status, name);
CREATE INDEX IF NOT EXISTS idx_workbuddy_credentials_tenant_kind
  ON workbuddy_connector_credentials(tenant_id, connector_kind);

-- Append-only ledger: one row per credential write, numbered from 1.
CREATE TABLE IF NOT EXISTS workbuddy_connector_credential_revisions (
  credential_revision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              UUID NOT NULL,
  credential_id          UUID NOT NULL,
  revision               INTEGER NOT NULL,
  action                 TEXT NOT NULL,
  external_ref           TEXT NOT NULL,
  actor_membership_id    UUID NOT NULL,
  created_at             BIGINT NOT NULL,
  CONSTRAINT workbuddy_credential_revisions_tenant_id_key UNIQUE (tenant_id, credential_revision_id),
  CONSTRAINT workbuddy_credential_revisions_number_key UNIQUE (credential_id, revision),
  CONSTRAINT workbuddy_credential_revisions_action_check CHECK (action IN ('created', 'rotated', 'revoked')),
  CONSTRAINT workbuddy_credential_revisions_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_credential_revisions_external_ref_check CHECK (
    external_ref ~ '^[a-z][a-z0-9+.-]*://' AND char_length(external_ref) <= 512
  ),
  CONSTRAINT workbuddy_credential_revisions_credential_fkey FOREIGN KEY (tenant_id, credential_id)
    REFERENCES workbuddy_connector_credentials(tenant_id, credential_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_credential_revisions_tenant
  ON workbuddy_connector_credential_revisions(tenant_id, credential_id, revision DESC);

-- Member level access to one tenant credential. The composite FK on
-- (tenant_id, membership_id) makes a cross-tenant grant unrepresentable.
CREATE TABLE IF NOT EXISTS workbuddy_connector_credential_grants (
  grant_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL,
  credential_id            UUID NOT NULL,
  membership_id            UUID NOT NULL,
  granted_by_membership_id UUID NOT NULL,
  granted_at               BIGINT NOT NULL,
  CONSTRAINT workbuddy_credential_grants_target_key UNIQUE (tenant_id, credential_id, membership_id),
  CONSTRAINT workbuddy_credential_grants_credential_fkey FOREIGN KEY (tenant_id, credential_id)
    REFERENCES workbuddy_connector_credentials(tenant_id, credential_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_credential_grants_member_fkey FOREIGN KEY (tenant_id, membership_id)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_credential_grants_member
  ON workbuddy_connector_credential_grants(tenant_id, membership_id);

-- ── Platform catalog: fixed tool and model revisions (global, no tenant) ─────

CREATE TABLE IF NOT EXISTS workbuddy_platform_tool_revisions (
  tool_revision_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  adapter_key          TEXT NOT NULL,
  tool_key             TEXT NOT NULL,
  revision             INTEGER NOT NULL,
  display_name         TEXT NOT NULL,
  description          TEXT NOT NULL DEFAULT '',
  status               TEXT NOT NULL DEFAULT 'published',
  published_by_user_id INTEGER NOT NULL,
  published_at         BIGINT NOT NULL,
  revoked_by_user_id   INTEGER,
  revoked_at           BIGINT,
  CONSTRAINT workbuddy_platform_tool_revisions_revision_key UNIQUE (adapter_key, tool_key, revision),
  CONSTRAINT workbuddy_platform_tool_revisions_status_check CHECK (status IN ('published', 'revoked')),
  CONSTRAINT workbuddy_platform_tool_revisions_adapter_key_check CHECK (char_length(btrim(adapter_key)) BETWEEN 1 AND 120),
  CONSTRAINT workbuddy_platform_tool_revisions_tool_key_check CHECK (char_length(btrim(tool_key)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_platform_tool_revisions_display_name_check CHECK (char_length(btrim(display_name)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_platform_tool_revisions_description_check CHECK (char_length(description) <= 1000),
  CONSTRAINT workbuddy_platform_tool_revisions_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_platform_tool_revisions_revocation_check CHECK (
    (status = 'revoked') = (revoked_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS workbuddy_platform_model_revisions (
  model_revision_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  adapter_key          TEXT NOT NULL,
  model_key            TEXT NOT NULL,
  revision             INTEGER NOT NULL,
  display_name         TEXT NOT NULL,
  description          TEXT NOT NULL DEFAULT '',
  status               TEXT NOT NULL DEFAULT 'published',
  published_by_user_id INTEGER NOT NULL,
  published_at         BIGINT NOT NULL,
  revoked_by_user_id   INTEGER,
  revoked_at           BIGINT,
  CONSTRAINT workbuddy_platform_model_revisions_revision_key UNIQUE (adapter_key, model_key, revision),
  CONSTRAINT workbuddy_platform_model_revisions_status_check CHECK (status IN ('published', 'revoked')),
  CONSTRAINT workbuddy_platform_model_revisions_adapter_key_check CHECK (char_length(btrim(adapter_key)) BETWEEN 1 AND 120),
  CONSTRAINT workbuddy_platform_model_revisions_model_key_check CHECK (char_length(btrim(model_key)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_platform_model_revisions_display_name_check CHECK (char_length(btrim(display_name)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_platform_model_revisions_description_check CHECK (char_length(description) <= 1000),
  CONSTRAINT workbuddy_platform_model_revisions_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_platform_model_revisions_revocation_check CHECK (
    (status = 'revoked') = (revoked_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_platform_tool_revisions_key
  ON workbuddy_platform_tool_revisions(adapter_key, tool_key, revision DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_platform_tool_revisions_status
  ON workbuddy_platform_tool_revisions(status);
CREATE INDEX IF NOT EXISTS idx_workbuddy_platform_model_revisions_key
  ON workbuddy_platform_model_revisions(adapter_key, model_key, revision DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_platform_model_revisions_status
  ON workbuddy_platform_model_revisions(status);

-- ── Tenant capabilities: approved revisions plus the default model ───────────

CREATE TABLE IF NOT EXISTS workbuddy_tenant_capabilities (
  tenant_id                 UUID PRIMARY KEY REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  revision                  INTEGER NOT NULL DEFAULT 1,
  default_model_revision_id UUID,
  updated_at                BIGINT NOT NULL,
  updated_by_membership_id  UUID,
  CONSTRAINT workbuddy_tenant_capabilities_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_tenant_capabilities_default_model_fkey FOREIGN KEY (default_model_revision_id)
    REFERENCES workbuddy_platform_model_revisions(model_revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS workbuddy_tenant_tool_grants (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  tool_revision_id         UUID NOT NULL REFERENCES workbuddy_platform_tool_revisions(tool_revision_id) ON DELETE CASCADE,
  granted_by_membership_id UUID,
  granted_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, tool_revision_id)
);

CREATE TABLE IF NOT EXISTS workbuddy_tenant_model_grants (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  model_revision_id        UUID NOT NULL REFERENCES workbuddy_platform_model_revisions(model_revision_id) ON DELETE CASCADE,
  granted_by_membership_id UUID,
  granted_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, model_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_tenant_tool_grants_revision
  ON workbuddy_tenant_tool_grants(tool_revision_id);
CREATE INDEX IF NOT EXISTS idx_workbuddy_tenant_model_grants_revision
  ON workbuddy_tenant_model_grants(model_revision_id);

-- ── Guard triggers ───────────────────────────────────────────────────────────

-- A credential row keeps its identity, steps one revision per write, and its
-- revocation is terminal; only the descriptor and status columns may change.
CREATE OR REPLACE FUNCTION workbuddy_catalog_guard_credential_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  IF NEW.credential_id <> OLD.credential_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.created_at <> OLD.created_at OR NEW.owner_membership_id <> OLD.owner_membership_id THEN RAISE EXCEPTION 'workbuddy: credential identity is immutable' USING ERRCODE = '23514'; END IF; -- public id, tenant, creation stamp and owner never change
  IF OLD.status = 'revoked' AND NEW.status <> 'revoked' THEN RAISE EXCEPTION 'workbuddy: credential revocation is terminal' USING ERRCODE = '23514'; END IF; -- a revoked credential never comes back
  IF NEW.revision <> OLD.revision + 1 THEN RAISE EXCEPTION 'workbuddy: credential revision must advance by one' USING ERRCODE = '23514'; END IF; -- ledger stays gapless
  RETURN NEW; -- accepted
END $wb$;

-- Platform catalog rows are fixed revisions: the descriptor is frozen, deletes
-- and truncates are rejected, and only a one-way revocation may pass.
CREATE OR REPLACE FUNCTION workbuddy_catalog_guard_platform_revision() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN RAISE EXCEPTION 'workbuddy: platform revisions are never removed' USING ERRCODE = '23514'; END IF; -- revisions are permanent
  IF (to_jsonb(NEW) - 'status' - 'revoked_at' - 'revoked_by_user_id') IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'revoked_at' - 'revoked_by_user_id') THEN RAISE EXCEPTION 'workbuddy: platform revision descriptor is immutable' USING ERRCODE = '23514'; END IF; -- adapter, keys and display metadata frozen
  IF OLD.status = 'revoked' OR NEW.status <> 'revoked' THEN RAISE EXCEPTION 'workbuddy: platform revision status is one-way' USING ERRCODE = '23514'; END IF; -- publish once, revoke once
  RETURN NEW; -- accepted
END $wb$;

-- Capability grants may only reference published tool or model revisions.
CREATE OR REPLACE FUNCTION workbuddy_catalog_guard_capability_grant() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
DECLARE approved boolean; -- resolved revision state
BEGIN
  IF TG_TABLE_NAME = 'workbuddy_tenant_tool_grants' THEN -- tool grant
    SELECT (status = 'published') INTO approved FROM workbuddy_platform_tool_revisions WHERE tool_revision_id = NEW.tool_revision_id; -- resolve the tool revision
  ELSE -- model grant
    SELECT (status = 'published') INTO approved FROM workbuddy_platform_model_revisions WHERE model_revision_id = NEW.model_revision_id; -- resolve the model revision
  END IF; -- table resolved
  IF approved IS NOT TRUE THEN RAISE EXCEPTION 'workbuddy: capability grant references an unpublished revision' USING ERRCODE = '23514'; END IF; -- revoked or unknown revisions are not grantable
  RETURN NEW; -- accepted
END $wb$;

-- A tenant default model must itself be a published revision.
CREATE OR REPLACE FUNCTION workbuddy_catalog_guard_capability_default() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
DECLARE approved boolean; -- resolved revision state
BEGIN
  IF NEW.default_model_revision_id IS NULL THEN RETURN NEW; END IF; -- no default configured
  SELECT (status = 'published') INTO approved FROM workbuddy_platform_model_revisions WHERE model_revision_id = NEW.default_model_revision_id; -- resolve the default revision
  IF approved IS NOT TRUE THEN RAISE EXCEPTION 'workbuddy: default model must be a published revision' USING ERRCODE = '23514'; END IF; -- stale defaults rejected
  RETURN NEW; -- accepted
END $wb$;

DROP TRIGGER IF EXISTS workbuddy_credentials_guard_mutation ON workbuddy_connector_credentials;
CREATE TRIGGER workbuddy_credentials_guard_mutation BEFORE UPDATE ON workbuddy_connector_credentials
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_credential_mutation();

DROP TRIGGER IF EXISTS workbuddy_credential_revisions_append_only ON workbuddy_connector_credential_revisions;
CREATE TRIGGER workbuddy_credential_revisions_append_only BEFORE UPDATE OR DELETE ON workbuddy_connector_credential_revisions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_credential_revisions_no_truncate ON workbuddy_connector_credential_revisions;
CREATE TRIGGER workbuddy_credential_revisions_no_truncate BEFORE TRUNCATE ON workbuddy_connector_credential_revisions
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_platform_tool_revisions_immutable ON workbuddy_platform_tool_revisions;
CREATE TRIGGER workbuddy_platform_tool_revisions_immutable BEFORE UPDATE OR DELETE ON workbuddy_platform_tool_revisions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_platform_revision();

DROP TRIGGER IF EXISTS workbuddy_platform_tool_revisions_no_truncate ON workbuddy_platform_tool_revisions;
CREATE TRIGGER workbuddy_platform_tool_revisions_no_truncate BEFORE TRUNCATE ON workbuddy_platform_tool_revisions
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_catalog_guard_platform_revision();

DROP TRIGGER IF EXISTS workbuddy_platform_model_revisions_immutable ON workbuddy_platform_model_revisions;
CREATE TRIGGER workbuddy_platform_model_revisions_immutable BEFORE UPDATE OR DELETE ON workbuddy_platform_model_revisions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_platform_revision();

DROP TRIGGER IF EXISTS workbuddy_platform_model_revisions_no_truncate ON workbuddy_platform_model_revisions;
CREATE TRIGGER workbuddy_platform_model_revisions_no_truncate BEFORE TRUNCATE ON workbuddy_platform_model_revisions
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_catalog_guard_platform_revision();

DROP TRIGGER IF EXISTS workbuddy_tool_grants_published_only ON workbuddy_tenant_tool_grants;
CREATE TRIGGER workbuddy_tool_grants_published_only BEFORE INSERT OR UPDATE ON workbuddy_tenant_tool_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_capability_grant();

DROP TRIGGER IF EXISTS workbuddy_model_grants_published_only ON workbuddy_tenant_model_grants;
CREATE TRIGGER workbuddy_model_grants_published_only BEFORE INSERT OR UPDATE ON workbuddy_tenant_model_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_capability_grant();

DROP TRIGGER IF EXISTS workbuddy_capabilities_default_published ON workbuddy_tenant_capabilities;
CREATE TRIGGER workbuddy_capabilities_default_published BEFORE INSERT OR UPDATE ON workbuddy_tenant_capabilities
  FOR EACH ROW EXECUTE FUNCTION workbuddy_catalog_guard_capability_default();

-- ── Row level security ───────────────────────────────────────────────────────

ALTER TABLE workbuddy_connector_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_connector_credentials FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_credentials_tenant_isolation ON workbuddy_connector_credentials;
CREATE POLICY workbuddy_credentials_tenant_isolation ON workbuddy_connector_credentials USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_connector_credential_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_connector_credential_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_credential_revisions_tenant_isolation ON workbuddy_connector_credential_revisions;
CREATE POLICY workbuddy_credential_revisions_tenant_isolation ON workbuddy_connector_credential_revisions USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_connector_credential_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_connector_credential_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_credential_grants_tenant_isolation ON workbuddy_connector_credential_grants;
CREATE POLICY workbuddy_credential_grants_tenant_isolation ON workbuddy_connector_credential_grants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_platform_tool_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_platform_tool_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_platform_tool_revisions_read ON workbuddy_platform_tool_revisions;
CREATE POLICY workbuddy_platform_tool_revisions_read ON workbuddy_platform_tool_revisions FOR SELECT USING (true);
DROP POLICY IF EXISTS workbuddy_platform_tool_revisions_publish ON workbuddy_platform_tool_revisions;
CREATE POLICY workbuddy_platform_tool_revisions_publish ON workbuddy_platform_tool_revisions USING (workbuddy_system_context()) WITH CHECK (workbuddy_system_context());

ALTER TABLE workbuddy_platform_model_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_platform_model_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_platform_model_revisions_read ON workbuddy_platform_model_revisions;
CREATE POLICY workbuddy_platform_model_revisions_read ON workbuddy_platform_model_revisions FOR SELECT USING (true);
DROP POLICY IF EXISTS workbuddy_platform_model_revisions_publish ON workbuddy_platform_model_revisions;
CREATE POLICY workbuddy_platform_model_revisions_publish ON workbuddy_platform_model_revisions USING (workbuddy_system_context()) WITH CHECK (workbuddy_system_context());

ALTER TABLE workbuddy_tenant_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_capabilities FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_capabilities_tenant_isolation ON workbuddy_tenant_capabilities;
CREATE POLICY workbuddy_capabilities_tenant_isolation ON workbuddy_tenant_capabilities USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_tool_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_tool_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tool_grants_tenant_isolation ON workbuddy_tenant_tool_grants;
CREATE POLICY workbuddy_tool_grants_tenant_isolation ON workbuddy_tenant_tool_grants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_model_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_model_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_model_grants_tenant_isolation ON workbuddy_tenant_model_grants;
CREATE POLICY workbuddy_model_grants_tenant_isolation ON workbuddy_tenant_model_grants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_connector_credentials FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_connector_credential_revisions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_connector_credential_grants FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_platform_tool_revisions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_platform_model_revisions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_capabilities FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_tool_grants FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_model_grants FROM PUBLIC;

REVOKE ALL ON FUNCTION workbuddy_catalog_guard_credential_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_catalog_guard_platform_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_catalog_guard_capability_grant() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_catalog_guard_capability_default() FROM PUBLIC;

UPDATE _schema_version SET version = 16;
