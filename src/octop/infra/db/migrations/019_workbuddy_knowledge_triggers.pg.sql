-- Schema v19: WorkBuddy knowledge bases, document generations and trigger
-- registrations (PostgreSQL only).
--
-- Scope: enterprise-scoped retrieval for WorkBuddy workflows.
--   * knowledge bases are personal (owner only), department (current members)
--     or enterprise (every active tenant member); tenant admins get no
--     implicit access to another member's personal base;
--   * explicit ACL rows are additive and are read on every transaction, so a
--     revoked grant stops matching immediately;
--   * documents publish exactly one ready generation; a generation is either
--     fully visible (row + chunks) or invisible, and only the document's
--     active generation is searchable;
--   * chunks store pgvector 1024-dimension embeddings produced by the pinned,
--     tenant-granted bge-m3 platform model revision;
--   * trigger registrations keep only external secret references (never the
--     signing secret), grants for tools and knowledge bases, and a persistent
--     event-key ledger so a replayed webhook can never execute twice.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on the regex "; \s* \n", so each top-level statement
--     must end its line with a semicolon, and a plpgsql body must never contain
--     a semicolon directly followed by a newline: end such a line with a
--     trailing comment ("; -- ...") so the split cannot land inside the body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- pgvector supplies the 1024-dimension embedding column type. It is provisioned
-- outside this migration (docker/postgres/init-vector.sql for the local image;
-- the provider or DBA for managed PostgreSQL) exactly like the rest of the
-- control-plane prerequisites, so this file never issues CREATE EXTENSION.
-- When the extension is missing the migration aborts instead of silently
-- storing unsearchable documents.

-- ── Knowledge bases ──────────────────────────────────────────────────────────
-- scope personal   → owner_user_id only, no implicit tenant admin read
-- scope department → department_id only, current members of that department
-- scope enterprise → every active member of the tenant

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_bases (
  tenant_id                   UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  kb_id                       UUID NOT NULL DEFAULT gen_random_uuid(),
  scope                       TEXT NOT NULL,
  owner_user_id               INTEGER REFERENCES users(id) ON DELETE SET NULL,
  department_id               UUID,
  name                        TEXT NOT NULL,
  description                 TEXT NOT NULL DEFAULT '',
  embedding_model_revision_id UUID NOT NULL REFERENCES workbuddy_platform_model_revisions(model_revision_id) ON DELETE RESTRICT,
  embedding_adapter_key       TEXT NOT NULL,
  embedding_model_key         TEXT NOT NULL,
  embedding_revision          INTEGER NOT NULL,
  embedding_dimensions        INTEGER NOT NULL DEFAULT 1024,
  archived_at                 BIGINT,
  archived_by_user_id         INTEGER,
  created_by_user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at                  BIGINT NOT NULL,
  updated_at                  BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, kb_id),
  CONSTRAINT wb_knowledge_bases_scope_valid CHECK (scope IN ('personal', 'department', 'enterprise')),
  CONSTRAINT wb_knowledge_bases_scope_shape CHECK (
    (scope = 'personal' AND owner_user_id IS NOT NULL AND department_id IS NULL)
    OR (scope = 'department' AND department_id IS NOT NULL AND owner_user_id IS NULL)
    OR (scope = 'enterprise' AND owner_user_id IS NULL AND department_id IS NULL)
  ),
  CONSTRAINT wb_knowledge_bases_dimensions_fixed CHECK (embedding_dimensions = 1024),
  CONSTRAINT wb_knowledge_bases_revision_positive CHECK (embedding_revision >= 1),
  CONSTRAINT wb_knowledge_bases_name_length CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT wb_knowledge_bases_description_length CHECK (char_length(description) <= 2000),
  CONSTRAINT wb_knowledge_bases_archive_coherent CHECK ((archived_at IS NULL) = (archived_by_user_id IS NULL)),
  CONSTRAINT wb_knowledge_bases_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS wb_knowledge_bases_tenant_scope_idx
  ON workbuddy_knowledge_bases(tenant_id, scope, created_at DESC);
CREATE INDEX IF NOT EXISTS wb_knowledge_bases_tenant_owner_idx
  ON workbuddy_knowledge_bases(tenant_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wb_knowledge_bases_tenant_department_idx
  ON workbuddy_knowledge_bases(tenant_id, department_id) WHERE department_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wb_knowledge_bases_revision_idx
  ON workbuddy_knowledge_bases(embedding_model_revision_id);

-- ── Explicit ACL grants (additive; exactly one user or department subject) ───

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_acl (
  tenant_id     UUID NOT NULL,
  acl_id        UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id         UUID NOT NULL,
  user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
  department_id UUID,
  permission    TEXT NOT NULL,
  granted_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at    BIGINT NOT NULL,
  updated_at    BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, acl_id),
  CONSTRAINT wb_knowledge_acl_permission_valid CHECK (permission IN ('read', 'write', 'admin')),
  CONSTRAINT wb_knowledge_acl_subject_exactly_one CHECK ((user_id IS NOT NULL) <> (department_id IS NOT NULL)),
  CONSTRAINT wb_knowledge_acl_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_acl_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS wb_knowledge_acl_user_idx
  ON workbuddy_knowledge_acl(tenant_id, kb_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS wb_knowledge_acl_department_idx
  ON workbuddy_knowledge_acl(tenant_id, kb_id, department_id) WHERE department_id IS NOT NULL;

-- ── Bound uploads and completed file references ──────────────────────────────
-- object_key is opaque storage detail; the file reference is the only thing a
-- document may consume and it is bound to tenant + base + upload.

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_uploads (
  tenant_id       UUID NOT NULL,
  upload_id       UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id           UUID NOT NULL,
  requested_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  filename        TEXT NOT NULL,
  mime_type       TEXT NOT NULL,
  size_bytes      BIGINT NOT NULL,
  object_key      TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  checksum_sha256 TEXT,
  detected_mime   TEXT,
  scan_status     TEXT,
  file_ref_id     UUID,
  rejection_code  TEXT,
  created_at      BIGINT NOT NULL,
  expires_at      BIGINT NOT NULL,
  completed_at    BIGINT,
  PRIMARY KEY (tenant_id, upload_id),
  CONSTRAINT wb_knowledge_uploads_status_valid CHECK (status IN ('pending', 'completed', 'rejected', 'expired')),
  CONSTRAINT wb_knowledge_uploads_size_positive CHECK (size_bytes > 0),
  CONSTRAINT wb_knowledge_uploads_scan_valid CHECK (scan_status IS NULL OR scan_status IN ('clean', 'infected', 'unsupported')),
  CONSTRAINT wb_knowledge_uploads_object_key_length CHECK (char_length(object_key) BETWEEN 1 AND 512),
  CONSTRAINT wb_knowledge_uploads_filename_length CHECK (char_length(filename) BETWEEN 1 AND 255),
  CONSTRAINT wb_knowledge_uploads_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wb_knowledge_uploads_pending_idx
  ON workbuddy_knowledge_uploads(tenant_id, kb_id, status, expires_at);

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_file_refs (
  tenant_id       UUID NOT NULL,
  file_ref_id     UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id           UUID NOT NULL,
  upload_id       UUID NOT NULL,
  object_key      TEXT NOT NULL,
  filename        TEXT NOT NULL,
  mime_type       TEXT NOT NULL,
  size_bytes      BIGINT NOT NULL,
  checksum_sha256 TEXT NOT NULL,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at      BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, file_ref_id),
  CONSTRAINT wb_knowledge_file_refs_size_positive CHECK (size_bytes > 0),
  CONSTRAINT wb_knowledge_file_refs_checksum_length CHECK (char_length(checksum_sha256) = 64),
  CONSTRAINT wb_knowledge_file_refs_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_file_refs_upload_fkey FOREIGN KEY (tenant_id, upload_id)
    REFERENCES workbuddy_knowledge_uploads(tenant_id, upload_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_file_refs_upload_unique UNIQUE (tenant_id, upload_id)
);

-- ── Documents (one active, published generation) ─────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_documents (
  tenant_id       UUID NOT NULL,
  document_id     UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id           UUID NOT NULL,
  file_ref_id     UUID NOT NULL,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  error_code      TEXT,
  active_generation_id UUID,
  chunk_count     INTEGER NOT NULL DEFAULT 0,
  job_id          UUID NOT NULL,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at      BIGINT NOT NULL,
  updated_at      BIGINT NOT NULL,
  deleted_at      BIGINT,
  PRIMARY KEY (tenant_id, document_id),
  CONSTRAINT wb_knowledge_documents_title_length CHECK (char_length(btrim(title)) BETWEEN 1 AND 255),
  CONSTRAINT wb_knowledge_documents_status_valid CHECK (status IN ('pending', 'parsing', 'indexing', 'ready', 'failed', 'deleted')),
  CONSTRAINT wb_knowledge_documents_chunk_count_valid CHECK (chunk_count >= 0),
  CONSTRAINT wb_knowledge_documents_ready_has_generation CHECK (status <> 'ready' OR active_generation_id IS NOT NULL),
  CONSTRAINT wb_knowledge_documents_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_documents_file_ref_fkey FOREIGN KEY (tenant_id, file_ref_id)
    REFERENCES workbuddy_knowledge_file_refs(tenant_id, file_ref_id) ON DELETE RESTRICT,
  CONSTRAINT wb_knowledge_documents_file_ref_unique UNIQUE (tenant_id, kb_id, file_ref_id)
);

CREATE INDEX IF NOT EXISTS wb_knowledge_documents_kb_status_idx
  ON workbuddy_knowledge_documents(tenant_id, kb_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS wb_knowledge_documents_job_idx
  ON workbuddy_knowledge_documents(tenant_id, job_id);

-- ── Immutable generations: a ready generation is the published unit ──────────

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_generations (
  tenant_id       UUID NOT NULL,
  generation_id   UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id           UUID NOT NULL,
  document_id     UUID NOT NULL,
  generation_number INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'ready',
  embedding_model_revision_id UUID NOT NULL,
  embedding_adapter_key TEXT NOT NULL,
  embedding_model_key   TEXT NOT NULL,
  embedding_revision    INTEGER NOT NULL,
  embedding_dimensions  INTEGER NOT NULL,
  chunk_count     INTEGER NOT NULL DEFAULT 0,
  error_code      TEXT,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at      BIGINT NOT NULL,
  ready_at        BIGINT,
  PRIMARY KEY (tenant_id, generation_id),
  CONSTRAINT wb_knowledge_generations_number_positive CHECK (generation_number >= 1),
  CONSTRAINT wb_knowledge_generations_status_valid CHECK (status IN ('building', 'ready', 'failed')),
  CONSTRAINT wb_knowledge_generations_dimensions_fixed CHECK (embedding_dimensions = 1024),
  CONSTRAINT wb_knowledge_generations_chunk_count_valid CHECK (chunk_count >= 0),
  CONSTRAINT wb_knowledge_generations_ready_coherent CHECK ((status = 'ready') = (ready_at IS NOT NULL)),
  CONSTRAINT wb_knowledge_generations_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_generations_document_fkey FOREIGN KEY (tenant_id, document_id)
    REFERENCES workbuddy_knowledge_documents(tenant_id, document_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_generations_number_unique UNIQUE (tenant_id, document_id, generation_number),
  CONSTRAINT wb_knowledge_generations_model_revision_fkey FOREIGN KEY (embedding_model_revision_id)
    REFERENCES workbuddy_platform_model_revisions(model_revision_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS wb_knowledge_generations_document_idx
  ON workbuddy_knowledge_generations(tenant_id, document_id, generation_number DESC);

-- ── Chunks: pgvector embeddings for the pinned 1024-dimension model ──────────

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_chunks (
  tenant_id     UUID NOT NULL,
  chunk_id      UUID NOT NULL DEFAULT gen_random_uuid(),
  kb_id         UUID NOT NULL,
  document_id   UUID NOT NULL,
  generation_id UUID NOT NULL,
  ordinal       INTEGER NOT NULL,
  content       TEXT NOT NULL,
  token_count   INTEGER NOT NULL DEFAULT 0,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding     vector(1024) NOT NULL,
  created_at    BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, chunk_id),
  CONSTRAINT wb_knowledge_chunks_ordinal_valid CHECK (ordinal >= 0),
  CONSTRAINT wb_knowledge_chunks_token_count_valid CHECK (token_count >= 0),
  CONSTRAINT wb_knowledge_chunks_content_present CHECK (btrim(content) <> ''),
  CONSTRAINT wb_knowledge_chunks_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_chunks_document_fkey FOREIGN KEY (tenant_id, document_id)
    REFERENCES workbuddy_knowledge_documents(tenant_id, document_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_chunks_generation_fkey FOREIGN KEY (tenant_id, generation_id)
    REFERENCES workbuddy_knowledge_generations(tenant_id, generation_id) ON DELETE CASCADE,
  CONSTRAINT wb_knowledge_chunks_ordinal_unique UNIQUE (tenant_id, generation_id, ordinal)
);

CREATE INDEX IF NOT EXISTS wb_knowledge_chunks_generation_idx
  ON workbuddy_knowledge_chunks(tenant_id, kb_id, generation_id);
CREATE INDEX IF NOT EXISTS wb_knowledge_chunks_document_idx
  ON workbuddy_knowledge_chunks(tenant_id, document_id);
CREATE INDEX IF NOT EXISTS wb_knowledge_chunks_embedding_idx
  ON workbuddy_knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- The document's active generation is a same-tenant reference; the column is
-- filled inside the publish transaction, after the generation row exists.
ALTER TABLE workbuddy_knowledge_documents
  DROP CONSTRAINT IF EXISTS wb_knowledge_documents_active_generation_fkey;
ALTER TABLE workbuddy_knowledge_documents
  ADD CONSTRAINT wb_knowledge_documents_active_generation_fkey FOREIGN KEY (tenant_id, active_generation_id)
  REFERENCES workbuddy_knowledge_generations(tenant_id, generation_id) ON DELETE NO ACTION;

-- ── Trigger registrations (cron / webhook / event) ───────────────────────────
-- Only the external secret reference is stored; the signing secret itself
-- lives in the tenant secret backend and is never written to PostgreSQL.

CREATE TABLE IF NOT EXISTS workbuddy_trigger_registrations (
  tenant_id       UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  registration_id UUID NOT NULL DEFAULT gen_random_uuid(),
  workflow_id     UUID NOT NULL,
  kind            TEXT NOT NULL,
  name            TEXT NOT NULL,
  enabled         BOOLEAN NOT NULL DEFAULT TRUE,
  webhook_path    TEXT,
  cron_expression TEXT,
  event_name      TEXT,
  event_filter    JSONB NOT NULL DEFAULT '{}'::jsonb,
  secret_provider TEXT,
  secret_ref      TEXT,
  secret_version  INTEGER NOT NULL DEFAULT 0,
  previous_secret_ref TEXT,
  previous_secret_expires_at BIGINT,
  signature_algorithm TEXT NOT NULL DEFAULT 'hmac-sha256',
  signature_header TEXT NOT NULL DEFAULT 'x-workbuddy-signature',
  timestamp_header TEXT NOT NULL DEFAULT 'x-workbuddy-timestamp',
  tolerance_seconds INTEGER NOT NULL DEFAULT 300,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at      BIGINT NOT NULL,
  updated_at      BIGINT NOT NULL,
  revoked_at      BIGINT,
  PRIMARY KEY (tenant_id, registration_id),
  CONSTRAINT wb_trigger_registrations_kind_valid CHECK (kind IN ('cron', 'webhook', 'event')),
  CONSTRAINT wb_trigger_registrations_shape CHECK (
    (kind = 'webhook' AND webhook_path IS NOT NULL AND secret_ref IS NOT NULL)
    OR (kind = 'cron' AND cron_expression IS NOT NULL AND webhook_path IS NULL AND secret_ref IS NULL)
    OR (kind = 'event' AND event_name IS NOT NULL AND webhook_path IS NULL AND secret_ref IS NULL)
  ),
  CONSTRAINT wb_trigger_registrations_webhook_path_shape CHECK (
    webhook_path IS NULL OR webhook_path ~ '^[A-Za-z0-9_-]{16,128}$'
  ),
  CONSTRAINT wb_trigger_registrations_name_length CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT wb_trigger_registrations_tolerance_range CHECK (tolerance_seconds BETWEEN 30 AND 3600),
  CONSTRAINT wb_trigger_registrations_secret_version_valid CHECK (secret_version >= 0),
  CONSTRAINT wb_trigger_registrations_previous_coherent CHECK (
    (previous_secret_ref IS NULL) = (previous_secret_expires_at IS NULL)
  ),
  CONSTRAINT wb_trigger_registrations_revocation_coherent CHECK (
    revoked_at IS NULL OR enabled = FALSE
  ),
  CONSTRAINT wb_trigger_registrations_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS wb_trigger_registrations_webhook_path_idx
  ON workbuddy_trigger_registrations(webhook_path) WHERE webhook_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS wb_trigger_registrations_workflow_idx
  ON workbuddy_trigger_registrations(tenant_id, workflow_id, created_at DESC);

-- ── Explicit capability grants for one registration ─────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_trigger_grants (
  tenant_id       UUID NOT NULL,
  grant_id        UUID NOT NULL DEFAULT gen_random_uuid(),
  registration_id UUID NOT NULL,
  capability      TEXT NOT NULL,
  tool_name       TEXT,
  kb_id           UUID,
  permission      TEXT,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at      BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, grant_id),
  CONSTRAINT wb_trigger_grants_capability_valid CHECK (capability IN ('tool', 'knowledge_base')),
  CONSTRAINT wb_trigger_grants_shape CHECK (
    (capability = 'tool' AND tool_name IS NOT NULL AND kb_id IS NULL AND permission IS NULL)
    OR (capability = 'knowledge_base' AND kb_id IS NOT NULL AND tool_name IS NULL
        AND permission IS NOT NULL AND permission IN ('read', 'write'))
  ),
  CONSTRAINT wb_trigger_grants_registration_fkey FOREIGN KEY (tenant_id, registration_id)
    REFERENCES workbuddy_trigger_registrations(tenant_id, registration_id) ON DELETE CASCADE,
  CONSTRAINT wb_trigger_grants_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS wb_trigger_grants_tool_idx
  ON workbuddy_trigger_grants(tenant_id, registration_id, tool_name) WHERE tool_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS wb_trigger_grants_kb_idx
  ON workbuddy_trigger_grants(tenant_id, registration_id, kb_id) WHERE kb_id IS NOT NULL;

-- ── Persistent event-key ledger ─────────────────────────────────────────────
-- (tenant_id, registration_id, event_key) is unique: an accepted delivery is
-- never dispatched twice, and the ledger survives restarts and secret rotation.

CREATE TABLE IF NOT EXISTS workbuddy_trigger_deliveries (
  tenant_id       UUID NOT NULL,
  delivery_id     UUID NOT NULL DEFAULT gen_random_uuid(),
  registration_id UUID NOT NULL,
  event_key       TEXT NOT NULL,
  event_name      TEXT,
  status          TEXT NOT NULL DEFAULT 'accepted',
  is_test         BOOLEAN NOT NULL DEFAULT FALSE,
  body_sha256     TEXT NOT NULL,
  signature_version INTEGER,
  signature_timestamp BIGINT,
  attempt         INTEGER NOT NULL DEFAULT 1,
  execution_id    UUID,
  rejection_code  TEXT,
  actor_user_id   INTEGER,
  received_at     BIGINT NOT NULL,
  completed_at    BIGINT,
  PRIMARY KEY (tenant_id, delivery_id),
  CONSTRAINT wb_trigger_deliveries_status_valid CHECK (status IN ('accepted', 'executed', 'failed', 'rejected')),
  CONSTRAINT wb_trigger_deliveries_event_key_length CHECK (char_length(event_key) BETWEEN 1 AND 200),
  CONSTRAINT wb_trigger_deliveries_body_hash_length CHECK (char_length(body_sha256) = 64),
  CONSTRAINT wb_trigger_deliveries_attempt_positive CHECK (attempt >= 1),
  CONSTRAINT wb_trigger_deliveries_registration_fkey FOREIGN KEY (tenant_id, registration_id)
    REFERENCES workbuddy_trigger_registrations(tenant_id, registration_id) ON DELETE CASCADE,
  CONSTRAINT wb_trigger_deliveries_event_key_unique UNIQUE (tenant_id, registration_id, event_key)
);

CREATE INDEX IF NOT EXISTS wb_trigger_deliveries_status_idx
  ON workbuddy_trigger_deliveries(tenant_id, registration_id, status, received_at DESC);

-- ── Guard functions ─────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION workbuddy_knowledge_guard_published_generation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $body$ -- ready generations are immutable
BEGIN
  IF OLD.status = 'ready' THEN RAISE EXCEPTION 'workbuddy_knowledge_generation_immutable' USING ERRCODE = '55000'; END IF; -- published rows never change
  RETURN NEW; -- only unreachable for published rows
END $body$;

CREATE OR REPLACE FUNCTION workbuddy_knowledge_reject_chunk_update() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $body$ -- stored embeddings are immutable
BEGIN
  RAISE EXCEPTION 'workbuddy_knowledge_chunk_immutable' USING ERRCODE = '55000'; -- chunks are replaced, never rewritten
END $body$;

CREATE OR REPLACE FUNCTION workbuddy_knowledge_reject_acl_tenant_change() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $body$ -- an ACL row stays in its base and tenant
BEGIN
  IF NEW.kb_id <> OLD.kb_id OR NEW.tenant_id <> OLD.tenant_id THEN RAISE EXCEPTION 'workbuddy_knowledge_acl_scope_immutable' USING ERRCODE = '55000'; END IF; -- subject rewritten in place only
  RETURN NEW; -- permission updates are allowed
END $body$;

-- ── Triggers ────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS wb_knowledge_bases_immutable_id ON workbuddy_knowledge_bases;
CREATE TRIGGER wb_knowledge_bases_immutable_id BEFORE UPDATE ON workbuddy_knowledge_bases
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('kb_id');

DROP TRIGGER IF EXISTS wb_knowledge_generations_ready_immutable ON workbuddy_knowledge_generations;
CREATE TRIGGER wb_knowledge_generations_ready_immutable BEFORE UPDATE ON workbuddy_knowledge_generations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_knowledge_guard_published_generation();

DROP TRIGGER IF EXISTS wb_knowledge_chunks_reject_update ON workbuddy_knowledge_chunks;
CREATE TRIGGER wb_knowledge_chunks_reject_update BEFORE UPDATE ON workbuddy_knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION workbuddy_knowledge_reject_chunk_update();

DROP TRIGGER IF EXISTS wb_knowledge_acl_scope_immutable ON workbuddy_knowledge_acl;
CREATE TRIGGER wb_knowledge_acl_scope_immutable BEFORE UPDATE ON workbuddy_knowledge_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_knowledge_reject_acl_tenant_change();

DROP TRIGGER IF EXISTS wb_trigger_registrations_immutable_id ON workbuddy_trigger_registrations;
CREATE TRIGGER wb_trigger_registrations_immutable_id BEFORE UPDATE ON workbuddy_trigger_registrations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('registration_id');

DROP TRIGGER IF EXISTS wb_trigger_grants_immutable_id ON workbuddy_trigger_grants;
CREATE TRIGGER wb_trigger_grants_immutable_id BEFORE UPDATE ON workbuddy_trigger_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('grant_id');

-- ── Row level security ──────────────────────────────────────────────────────

ALTER TABLE workbuddy_knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_bases FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_bases;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_bases USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_acl FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_acl;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_acl USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_uploads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_uploads;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_uploads USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_file_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_file_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_file_refs;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_file_refs USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_documents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_documents;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_documents USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_generations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_generations;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_generations USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_chunks;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_chunks USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_trigger_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_trigger_registrations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_trigger_registrations;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_trigger_registrations USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_trigger_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_trigger_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_trigger_grants;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_trigger_grants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_trigger_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_trigger_deliveries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_trigger_deliveries;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_trigger_deliveries USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privileges ──────────────────────────────────────────────────────────────

REVOKE ALL ON TABLE workbuddy_knowledge_bases FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_acl FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_uploads FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_file_refs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_documents FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_generations FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_knowledge_chunks FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_trigger_registrations FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_trigger_grants FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_trigger_deliveries FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION workbuddy_knowledge_guard_published_generation() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION workbuddy_knowledge_reject_chunk_update() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION workbuddy_knowledge_reject_acl_tenant_change() FROM PUBLIC;

UPDATE _schema_version SET version = 19;
