-- Schema v17: WorkBuddy workflow definitions, immutable versions, tool bindings
-- and revocations.
--
-- PostgreSQL only. Every tenant table carries UUID identifiers, compound
-- (tenant_id, parent_id) foreign keys so a row can never point at another
-- tenant's parent, ENABLE + FORCE ROW LEVEL SECURITY bound to the
-- transaction-local app.tenant_id setting, and REVOKE ALL FROM PUBLIC.
-- Versions, bindings and revocations are append-only: the guard triggers below
-- refuse UPDATE/DELETE/TRUNCATE, so a published revision can never mutate.
-- Requires PostgreSQL 13+ (gen_random_uuid is core since 13); no CREATE
-- EXTENSION is used because managed PostgreSQL usually forbids it.
--
-- SQLite installs apply the no-op marker 017_workbuddy_workflows.sql (comment
-- plus the schema-version bump) and octop.infra.db.workbuddy_context refuses to
-- serve WorkBuddy with a controlled WORKBUDDY_POSTGRES_REQUIRED error, so
-- WorkBuddy never runs on SQLite as an isolation fallback.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on dollar-quoted text and top-level semicolons, so a
--     plpgsql body must stay inside one $wb$ ... $wb$ block;
--   * end body lines that carry a semicolon with a trailing comment ("; -- ...")
--     so the splitter can never land inside the function body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- ── Workflows (mutable draft state; revision is the compare-and-swap unit) ───

CREATE TABLE IF NOT EXISTS workbuddy_workflows (
  workflow_id              UUID NOT NULL DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  name                     TEXT NOT NULL,
  description              TEXT,
  status                   TEXT NOT NULL DEFAULT 'draft',
  revision                 BIGINT NOT NULL DEFAULT 1,
  active_version_id        UUID,
  shadow_version_id        UUID,
  created_by               INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_by_membership_id UUID,
  created_at               BIGINT NOT NULL,
  updated_at               BIGINT NOT NULL,
  archived_at              BIGINT,
  CONSTRAINT workbuddy_workflows_pkey PRIMARY KEY (tenant_id, workflow_id),
  CONSTRAINT workbuddy_workflows_status_check CHECK (status IN ('draft', 'active', 'archived')),
  CONSTRAINT workbuddy_workflows_revision_check CHECK (revision >= 1),
  CONSTRAINT workbuddy_workflows_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_workflows_description_check CHECK (description IS NULL OR char_length(description) <= 2000),
  CONSTRAINT workbuddy_workflows_creator_fkey FOREIGN KEY (tenant_id, created_by_membership_id)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_workflows_tenant_status
  ON workbuddy_workflows(tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_workflows_tenant_updated
  ON workbuddy_workflows(tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_workflows_tenant_creator
  ON workbuddy_workflows(tenant_id, created_by);

-- ── Immutable versions (one canonical definition each, hash-pinned) ─────────

CREATE TABLE IF NOT EXISTS workbuddy_workflow_versions (
  workflow_version_id UUID NOT NULL DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  workflow_id         UUID NOT NULL,
  version_number      BIGINT NOT NULL,
  definition          JSONB NOT NULL,
  definition_sha256   TEXT NOT NULL,
  origin              TEXT NOT NULL DEFAULT 'save',
  base_version_id     UUID,
  source_version_id   UUID,
  change_summary      TEXT,
  created_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_by_membership_id UUID,
  created_at          BIGINT NOT NULL,
  CONSTRAINT workbuddy_workflow_versions_pkey PRIMARY KEY (tenant_id, workflow_version_id),
  CONSTRAINT workbuddy_workflow_versions_tenant_workflow_version_key
    UNIQUE (tenant_id, workflow_id, workflow_version_id),
  CONSTRAINT workbuddy_workflow_versions_number_key UNIQUE (tenant_id, workflow_id, version_number),
  CONSTRAINT workbuddy_workflow_versions_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_workflow_versions_origin_check
    CHECK (origin IN ('save', 'rollback', 'proposal', 'promotion', 'import')),
  CONSTRAINT workbuddy_workflow_versions_number_check CHECK (version_number >= 1),
  CONSTRAINT workbuddy_workflow_versions_hash_check CHECK (definition_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT workbuddy_workflow_versions_summary_check CHECK (change_summary IS NULL OR char_length(change_summary) <= 500),
  CONSTRAINT workbuddy_workflow_versions_definition_check CHECK (jsonb_typeof(definition) = 'object'),
  CONSTRAINT workbuddy_workflow_versions_base_fkey FOREIGN KEY (tenant_id, base_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE NO ACTION,
  CONSTRAINT workbuddy_workflow_versions_source_fkey FOREIGN KEY (tenant_id, source_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_workflow_versions_workflow
  ON workbuddy_workflow_versions(tenant_id, workflow_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_workflow_versions_origin
  ON workbuddy_workflow_versions(tenant_id, origin, created_at DESC);

-- Active/shadow pointers are compound so they can only name a version of the
-- same tenant; ON DELETE NO ACTION keeps the pointer from silently clearing.
DO $wb$ BEGIN
  ALTER TABLE workbuddy_workflows DROP CONSTRAINT IF EXISTS workbuddy_workflows_active_version_fkey;
  ALTER TABLE workbuddy_workflows ADD CONSTRAINT workbuddy_workflows_active_version_fkey
    FOREIGN KEY (tenant_id, active_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE NO ACTION;
  ALTER TABLE workbuddy_workflows DROP CONSTRAINT IF EXISTS workbuddy_workflows_shadow_version_fkey;
  ALTER TABLE workbuddy_workflows ADD CONSTRAINT workbuddy_workflows_shadow_version_fkey
    FOREIGN KEY (tenant_id, shadow_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE NO ACTION;
END $wb$;

-- ── Tool/credential bindings captured with a version ────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_workflow_version_tool_bindings (
  tenant_id              UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  workflow_version_id    UUID NOT NULL,
  binding_key            TEXT NOT NULL,
  tool_name              TEXT NOT NULL,
  credential_revision_id UUID,
  binding_sha256         TEXT NOT NULL,
  created_at             BIGINT NOT NULL,
  CONSTRAINT workbuddy_workflow_tool_bindings_pkey
    PRIMARY KEY (tenant_id, workflow_version_id, binding_key),
  CONSTRAINT workbuddy_workflow_tool_bindings_version_fkey FOREIGN KEY (tenant_id, workflow_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_workflow_tool_bindings_credential_fkey FOREIGN KEY (tenant_id, credential_revision_id)
    REFERENCES workbuddy_connector_credential_revisions(tenant_id, credential_revision_id) ON DELETE NO ACTION,
  CONSTRAINT workbuddy_workflow_tool_bindings_key_check CHECK (char_length(btrim(binding_key)) BETWEEN 1 AND 128),
  CONSTRAINT workbuddy_workflow_tool_bindings_tool_check CHECK (char_length(btrim(tool_name)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_workflow_tool_bindings_hash_check CHECK (binding_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_workflow_tool_bindings_version
  ON workbuddy_workflow_version_tool_bindings(tenant_id, workflow_version_id);

-- ── Revocations (append-only record that a version or workflow is withdrawn) ─

CREATE TABLE IF NOT EXISTS workbuddy_workflow_revocations (
  revocation_id       UUID NOT NULL DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  workflow_id         UUID NOT NULL,
  workflow_version_id UUID,
  reason              TEXT NOT NULL,
  revoked_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  revoked_by_membership_id UUID,
  revoked_at          BIGINT NOT NULL,
  CONSTRAINT workbuddy_workflow_revocations_pkey PRIMARY KEY (tenant_id, revocation_id),
  CONSTRAINT workbuddy_workflow_revocations_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_workflow_revocations_version_fkey FOREIGN KEY (tenant_id, workflow_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_version_id) ON DELETE NO ACTION,
  CONSTRAINT workbuddy_workflow_revocations_reason_check CHECK (char_length(btrim(reason)) BETWEEN 1 AND 500)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_workflow_revocations_workflow
  ON workbuddy_workflow_revocations(tenant_id, workflow_id, revoked_at DESC);

-- ── Triggers: immutable identifiers, append-only history ────────────────────

DROP TRIGGER IF EXISTS workbuddy_workflows_immutable_workflow_id ON workbuddy_workflows;
CREATE TRIGGER workbuddy_workflows_immutable_workflow_id BEFORE UPDATE ON workbuddy_workflows
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('workflow_id');

DROP TRIGGER IF EXISTS workbuddy_workflows_immutable_tenant_id ON workbuddy_workflows;
CREATE TRIGGER workbuddy_workflows_immutable_tenant_id BEFORE UPDATE ON workbuddy_workflows
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('tenant_id');

DROP TRIGGER IF EXISTS workbuddy_workflow_versions_append_only ON workbuddy_workflow_versions;
CREATE TRIGGER workbuddy_workflow_versions_append_only BEFORE UPDATE OR DELETE ON workbuddy_workflow_versions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_workflow_versions_no_truncate ON workbuddy_workflow_versions;
CREATE TRIGGER workbuddy_workflow_versions_no_truncate BEFORE TRUNCATE ON workbuddy_workflow_versions
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_workflow_tool_bindings_append_only ON workbuddy_workflow_version_tool_bindings;
CREATE TRIGGER workbuddy_workflow_tool_bindings_append_only BEFORE UPDATE OR DELETE ON workbuddy_workflow_version_tool_bindings
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_workflow_revocations_append_only ON workbuddy_workflow_revocations;
CREATE TRIGGER workbuddy_workflow_revocations_append_only BEFORE UPDATE OR DELETE ON workbuddy_workflow_revocations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Row level security: every table is isolated by app.tenant_id ────────────

ALTER TABLE workbuddy_workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_workflows FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_workflows_tenant_isolation ON workbuddy_workflows;
CREATE POLICY workbuddy_workflows_tenant_isolation ON workbuddy_workflows USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_workflow_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_workflow_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_workflow_versions_tenant_isolation ON workbuddy_workflow_versions;
CREATE POLICY workbuddy_workflow_versions_tenant_isolation ON workbuddy_workflow_versions USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_workflow_version_tool_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_workflow_version_tool_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_workflow_tool_bindings_tenant_isolation ON workbuddy_workflow_version_tool_bindings;
CREATE POLICY workbuddy_workflow_tool_bindings_tenant_isolation ON workbuddy_workflow_version_tool_bindings USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_workflow_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_workflow_revocations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_workflow_revocations_tenant_isolation ON workbuddy_workflow_revocations;
CREATE POLICY workbuddy_workflow_revocations_tenant_isolation ON workbuddy_workflow_revocations USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_workflows FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_workflow_versions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_workflow_version_tool_bindings FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_workflow_revocations FROM PUBLIC;

UPDATE _schema_version SET version = 17;
