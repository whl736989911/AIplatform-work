-- Schema v22: WorkBuddy tenant lifecycle — controlled data export, one-time
-- redeem tokens, deletion requests with 30-day cooling-off, tenant archive with
-- 90-day retention, purge ledger, tombstones, legal holds and the anonymized
-- usage-linkage contract.
--
-- PostgreSQL only. SQLite installs apply the no-op watermark
-- 022_workbuddy_lifecycle.sql and every WorkBuddy entry point fails closed in
-- octop.infra.db.workbuddy_context.workbuddy_transaction, so SQLite can never
-- act as an isolation fallback for tenant workflows.
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
-- Lifecycle (all timestamps are unix seconds, matching 015):
--   export job:       queued -> ready -> redeemed | expired | failed
--   deletion request: cooling_off -> archived -> purged, or cooling_off -> cancelled
--   active legal hold: blocks archive/purge progression and is released explicitly
--   purge:            deletes tenant data, writes the ledger tail and the tombstone

-- ── Controlled export jobs ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_export_jobs (
  export_job_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id               UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  requested_by            INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status                  TEXT NOT NULL DEFAULT 'queued',
  scope                   TEXT NOT NULL DEFAULT 'tenant',
  redaction_rules_version INTEGER NOT NULL DEFAULT 1,
  table_total             INTEGER NOT NULL DEFAULT 0,
  row_total               BIGINT NOT NULL DEFAULT 0,
  manifest_json           TEXT,
  manifest_sha256         TEXT,
  content_sha256          TEXT,
  redeem_expires_at       BIGINT NOT NULL,
  redeemed_at             BIGINT,
  redeemed_by             INTEGER REFERENCES users(id) ON DELETE SET NULL,
  failure_reason          TEXT,
  version                 INTEGER NOT NULL DEFAULT 1,
  created_at              BIGINT NOT NULL,
  updated_at              BIGINT NOT NULL,
  completed_at            BIGINT,
  CONSTRAINT workbuddy_export_jobs_id_key UNIQUE (tenant_id, export_job_id),
  CONSTRAINT workbuddy_export_jobs_status_check CHECK (status IN ('queued', 'ready', 'failed', 'redeemed', 'expired')),
  CONSTRAINT workbuddy_export_jobs_scope_check CHECK (scope IN ('tenant')),
  CONSTRAINT workbuddy_export_jobs_version_check CHECK (version >= 1),
  CONSTRAINT workbuddy_export_jobs_window_check CHECK (redeem_expires_at > created_at),
  CONSTRAINT workbuddy_export_jobs_ready_check CHECK (status IN ('queued', 'failed') OR (manifest_json IS NOT NULL AND manifest_sha256 IS NOT NULL)),
  CONSTRAINT workbuddy_export_jobs_failure_check CHECK (status <> 'failed' OR failure_reason IS NOT NULL),
  CONSTRAINT workbuddy_export_jobs_redeemed_check CHECK ((status = 'redeemed') = (redeemed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_export_jobs_tenant_created
  ON workbuddy_export_jobs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_export_jobs_retention
  ON workbuddy_export_jobs(redeem_expires_at) WHERE status IN ('ready', 'redeemed');

-- Exported payload parts, one row per exported table.  Redaction happens before
-- insert, so a row in this table never contains a secret, a hash of one, or an
-- encrypted payload.  Rows are deleted when the 72h redeem window closes.
CREATE TABLE IF NOT EXISTS workbuddy_export_artifacts (
  artifact_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID NOT NULL,
  export_job_id  UUID NOT NULL,
  table_name     TEXT NOT NULL,
  row_count      BIGINT NOT NULL DEFAULT 0,
  content_sha256 TEXT NOT NULL,
  payload_json   TEXT NOT NULL,
  created_at     BIGINT NOT NULL,
  CONSTRAINT workbuddy_export_artifacts_table_key UNIQUE (tenant_id, export_job_id, table_name),
  CONSTRAINT workbuddy_export_artifacts_id_key UNIQUE (tenant_id, artifact_id),
  CONSTRAINT workbuddy_export_artifacts_row_check CHECK (row_count >= 0),
  CONSTRAINT workbuddy_export_artifacts_job_fkey FOREIGN KEY (tenant_id, export_job_id)
    REFERENCES workbuddy_export_jobs(tenant_id, export_job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_export_artifacts_job
  ON workbuddy_export_artifacts(tenant_id, export_job_id);

-- One-time redeem tokens: only the sha256 of the token is stored, the 72h window
-- is fixed at job creation (a new challenge never extends it), and consumption
-- is a single conditional UPDATE (CAS) that flips consumed_at exactly once.
CREATE TABLE IF NOT EXISTS workbuddy_export_redeem_tokens (
  redeem_token_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL,
  export_job_id   UUID NOT NULL,
  token_sha256    TEXT NOT NULL,
  issued_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  issued_at       BIGINT NOT NULL,
  expires_at      BIGINT NOT NULL,
  revoked_at      BIGINT,
  consumed_at     BIGINT,
  consumed_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT workbuddy_export_redeem_tokens_hash_key UNIQUE (token_sha256),
  CONSTRAINT workbuddy_export_redeem_tokens_id_key UNIQUE (tenant_id, redeem_token_id),
  CONSTRAINT workbuddy_export_redeem_tokens_ttl_check CHECK (expires_at > issued_at AND expires_at <= issued_at + 259200),
  CONSTRAINT workbuddy_export_redeem_tokens_once_check CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
  CONSTRAINT workbuddy_export_redeem_tokens_job_fkey FOREIGN KEY (tenant_id, export_job_id)
    REFERENCES workbuddy_export_jobs(tenant_id, export_job_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workbuddy_export_redeem_tokens_live
  ON workbuddy_export_redeem_tokens(tenant_id, export_job_id)
  WHERE consumed_at IS NULL AND revoked_at IS NULL;

-- ── Deletion requests (30-day cooling-off) ───────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_deletion_requests (
  deletion_request_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE RESTRICT,
  stage                 TEXT NOT NULL DEFAULT 'cooling_off',
  requested_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  requested_at          BIGINT NOT NULL,
  cooling_off_ends_at   BIGINT NOT NULL,
  policy_sha256         TEXT NOT NULL,
  policy_expires_at     BIGINT NOT NULL,
  cancelled_at          BIGINT,
  cancelled_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  archived_at           BIGINT,
  archive_sha256        TEXT,
  archive_row_total     BIGINT,
  purge_due_at          BIGINT,
  purged_at             BIGINT,
  purged_ledger_sequence BIGINT,
  version               INTEGER NOT NULL DEFAULT 1,
  updated_at            BIGINT NOT NULL,
  CONSTRAINT workbuddy_deletion_requests_id_key UNIQUE (tenant_id, deletion_request_id),
  CONSTRAINT workbuddy_deletion_requests_stage_check CHECK (stage IN ('cooling_off', 'cancelled', 'archived', 'purged')),
  CONSTRAINT workbuddy_deletion_requests_version_check CHECK (version >= 1),
  CONSTRAINT workbuddy_deletion_requests_window_check CHECK (cooling_off_ends_at > requested_at),
  CONSTRAINT workbuddy_deletion_requests_policy_check CHECK (policy_expires_at > requested_at),
  CONSTRAINT workbuddy_deletion_requests_cancel_check CHECK ((stage = 'cancelled') = (cancelled_at IS NOT NULL)),
  CONSTRAINT workbuddy_deletion_requests_archive_check CHECK (archived_at IS NULL OR archived_at >= cooling_off_ends_at),
  CONSTRAINT workbuddy_deletion_requests_purge_check CHECK (purged_at IS NULL OR (stage = 'purged' AND archive_sha256 IS NOT NULL AND purge_due_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workbuddy_deletion_requests_active
  ON workbuddy_deletion_requests(tenant_id) WHERE stage IN ('cooling_off', 'archived');
CREATE INDEX IF NOT EXISTS idx_workbuddy_deletion_requests_due
  ON workbuddy_deletion_requests(stage, purge_due_at);
CREATE INDEX IF NOT EXISTS idx_workbuddy_deletion_requests_tenant_created
  ON workbuddy_deletion_requests(tenant_id, requested_at DESC);

-- ── Legal holds (block archive/purge until released) ─────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_legal_holds (
  legal_hold_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE RESTRICT,
  deletion_request_id UUID,
  matter_reference    TEXT NOT NULL,
  reason              TEXT NOT NULL,
  placed_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
  placed_by_label     TEXT,
  placed_at           BIGINT NOT NULL,
  released_at         BIGINT,
  released_by         INTEGER REFERENCES users(id) ON DELETE SET NULL,
  release_reason      TEXT,
  CONSTRAINT workbuddy_legal_holds_id_key UNIQUE (tenant_id, legal_hold_id),
  CONSTRAINT workbuddy_legal_holds_matter_check CHECK (char_length(btrim(matter_reference)) BETWEEN 1 AND 200),
  CONSTRAINT workbuddy_legal_holds_reason_check CHECK (char_length(btrim(reason)) BETWEEN 1 AND 1000),
  CONSTRAINT workbuddy_legal_holds_release_check CHECK ((released_at IS NULL) = (release_reason IS NULL)),
  CONSTRAINT workbuddy_legal_holds_release_time_check CHECK (released_at IS NULL OR released_at >= placed_at),
  CONSTRAINT workbuddy_legal_holds_request_fkey FOREIGN KEY (tenant_id, deletion_request_id)
    REFERENCES workbuddy_deletion_requests(tenant_id, deletion_request_id) ON DELETE NO ACTION
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workbuddy_legal_holds_active_request
  ON workbuddy_legal_holds(tenant_id, deletion_request_id)
  WHERE released_at IS NULL AND deletion_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workbuddy_legal_holds_active_tenant
  ON workbuddy_legal_holds(tenant_id) WHERE released_at IS NULL;

-- ── Archive (90-day retention before purge) ──────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_tenant_archives (
  archive_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL,
  deletion_request_id UUID NOT NULL,
  archive_sha256      TEXT NOT NULL,
  table_total         INTEGER NOT NULL DEFAULT 0,
  row_total           BIGINT NOT NULL DEFAULT 0,
  payload_json        TEXT NOT NULL,
  retained_until      BIGINT NOT NULL,
  purged_at           BIGINT,
  created_at          BIGINT NOT NULL,
  CONSTRAINT workbuddy_tenant_archives_id_key UNIQUE (tenant_id, archive_id),
  CONSTRAINT workbuddy_tenant_archives_request_key UNIQUE (tenant_id, deletion_request_id),
  CONSTRAINT workbuddy_tenant_archives_retention_check CHECK (retained_until >= created_at),
  CONSTRAINT workbuddy_tenant_archives_request_fkey FOREIGN KEY (tenant_id, deletion_request_id)
    REFERENCES workbuddy_deletion_requests(tenant_id, deletion_request_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_tenant_archives_retention
  ON workbuddy_tenant_archives(retained_until) WHERE purged_at IS NULL;

-- ── Deletion ledger (append only, hash chained) ──────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_deletion_ledger (
  ledger_entry_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE RESTRICT,
  deletion_request_id UUID,
  sequence            BIGINT NOT NULL,
  entry_type          TEXT NOT NULL,
  payload_json        TEXT NOT NULL DEFAULT '{}',
  payload_sha256      TEXT NOT NULL,
  previous_sha256     TEXT,
  entry_sha256        TEXT NOT NULL,
  actor_user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  actor_label         TEXT,
  created_at          BIGINT NOT NULL,
  CONSTRAINT workbuddy_deletion_ledger_sequence_key UNIQUE (tenant_id, sequence),
  CONSTRAINT workbuddy_deletion_ledger_id_key UNIQUE (tenant_id, ledger_entry_id),
  CONSTRAINT workbuddy_deletion_ledger_sequence_check CHECK (sequence >= 1),
  CONSTRAINT workbuddy_deletion_ledger_type_check CHECK (entry_type IN ('export_requested', 'export_redeem_issued', 'export_redeemed', 'export_expired', 'deletion_requested', 'deletion_cancelled', 'legal_hold_placed', 'legal_hold_released', 'archive_created', 'usage_linkage_anonymized', 'purge_completed', 'restore_replayed')),
  CONSTRAINT workbuddy_deletion_ledger_request_fkey FOREIGN KEY (tenant_id, deletion_request_id)
    REFERENCES workbuddy_deletion_requests(tenant_id, deletion_request_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_deletion_ledger_tenant_created
  ON workbuddy_deletion_ledger(tenant_id, created_at DESC);

-- ── Tombstones (survive purge, block resurrection) ───────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_tenant_tombstones (
  tenant_id            UUID PRIMARY KEY REFERENCES workbuddy_tenants(tenant_id) ON DELETE RESTRICT,
  deletion_request_id  UUID NOT NULL,
  purged_at            BIGINT NOT NULL,
  policy_sha256        TEXT NOT NULL,
  ledger_head_sha256   TEXT NOT NULL,
  ledger_entry_count   BIGINT NOT NULL,
  archive_sha256       TEXT,
  archive_row_total    BIGINT,
  usage_linkage_sha256 TEXT,
  purged_tables        INTEGER NOT NULL DEFAULT 0,
  purged_rows          BIGINT NOT NULL DEFAULT 0,
  retained_evidence    TEXT NOT NULL DEFAULT '[]',
  tombstone_sha256     TEXT NOT NULL,
  created_at           BIGINT NOT NULL,
  CONSTRAINT workbuddy_tenant_tombstones_request_fkey FOREIGN KEY (tenant_id, deletion_request_id)
    REFERENCES workbuddy_deletion_requests(tenant_id, deletion_request_id) ON DELETE NO ACTION
);

-- ── Anonymized usage linkage ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_usage_salts (
  salt_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  salt_ref     TEXT NOT NULL,
  salt_secret  BYTEA NOT NULL,
  created_at   BIGINT NOT NULL,
  destroyed_at BIGINT,
  CONSTRAINT workbuddy_usage_salts_ref_key UNIQUE (tenant_id, salt_ref),
  CONSTRAINT workbuddy_usage_salts_id_key UNIQUE (tenant_id, salt_id),
  CONSTRAINT workbuddy_usage_salts_ref_check CHECK (char_length(btrim(salt_ref)) BETWEEN 8 AND 64)
);

CREATE TABLE IF NOT EXISTS workbuddy_anonymized_usage_links (
  usage_link_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  salt_ref       TEXT NOT NULL,
  subject_kind   TEXT NOT NULL,
  subject_sha256 TEXT NOT NULL,
  event_count    BIGINT NOT NULL DEFAULT 0,
  first_seen_at  BIGINT NOT NULL,
  last_seen_at   BIGINT NOT NULL,
  purged_at      BIGINT,
  CONSTRAINT workbuddy_anonymized_usage_links_subject_key UNIQUE (tenant_id, subject_kind, subject_sha256),
  CONSTRAINT workbuddy_anonymized_usage_links_id_key UNIQUE (tenant_id, usage_link_id),
  CONSTRAINT workbuddy_anonymized_usage_links_kind_check CHECK (subject_kind IN ('user', 'agent', 'connector', 'workflow')),
  CONSTRAINT workbuddy_anonymized_usage_links_count_check CHECK (event_count >= 0),
  CONSTRAINT workbuddy_anonymized_usage_links_seen_check CHECK (last_seen_at >= first_seen_at)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_anonymized_usage_links_tenant
  ON workbuddy_anonymized_usage_links(tenant_id, subject_kind, last_seen_at DESC);

-- ── Triggers: immutable public ids, append-only ledger and tombstones ────────

DROP TRIGGER IF EXISTS workbuddy_export_jobs_immutable_id ON workbuddy_export_jobs;
CREATE TRIGGER workbuddy_export_jobs_immutable_id BEFORE UPDATE ON workbuddy_export_jobs
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('export_job_id');

DROP TRIGGER IF EXISTS workbuddy_export_artifacts_immutable_id ON workbuddy_export_artifacts;
CREATE TRIGGER workbuddy_export_artifacts_immutable_id BEFORE UPDATE ON workbuddy_export_artifacts
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('artifact_id');

DROP TRIGGER IF EXISTS workbuddy_export_redeem_tokens_immutable_id ON workbuddy_export_redeem_tokens;
CREATE TRIGGER workbuddy_export_redeem_tokens_immutable_id BEFORE UPDATE ON workbuddy_export_redeem_tokens
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('redeem_token_id');

DROP TRIGGER IF EXISTS workbuddy_deletion_requests_immutable_id ON workbuddy_deletion_requests;
CREATE TRIGGER workbuddy_deletion_requests_immutable_id BEFORE UPDATE ON workbuddy_deletion_requests
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('deletion_request_id');

DROP TRIGGER IF EXISTS workbuddy_legal_holds_immutable_id ON workbuddy_legal_holds;
CREATE TRIGGER workbuddy_legal_holds_immutable_id BEFORE UPDATE ON workbuddy_legal_holds
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('legal_hold_id');

DROP TRIGGER IF EXISTS workbuddy_tenant_archives_immutable_id ON workbuddy_tenant_archives;
CREATE TRIGGER workbuddy_tenant_archives_immutable_id BEFORE UPDATE ON workbuddy_tenant_archives
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('archive_id');

DROP TRIGGER IF EXISTS workbuddy_deletion_ledger_append_only ON workbuddy_deletion_ledger;
CREATE TRIGGER workbuddy_deletion_ledger_append_only BEFORE UPDATE OR DELETE ON workbuddy_deletion_ledger
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_deletion_ledger_no_truncate ON workbuddy_deletion_ledger;
CREATE TRIGGER workbuddy_deletion_ledger_no_truncate BEFORE TRUNCATE ON workbuddy_deletion_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_tenant_tombstones_append_only ON workbuddy_tenant_tombstones;
CREATE TRIGGER workbuddy_tenant_tombstones_append_only BEFORE UPDATE OR DELETE ON workbuddy_tenant_tombstones
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_tenant_tombstones_no_truncate ON workbuddy_tenant_tombstones;
CREATE TRIGGER workbuddy_tenant_tombstones_no_truncate BEFORE TRUNCATE ON workbuddy_tenant_tombstones
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Row level security: every tenant table is isolated by app.tenant_id ─────

ALTER TABLE workbuddy_export_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_export_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_export_jobs_tenant_isolation ON workbuddy_export_jobs;
CREATE POLICY workbuddy_export_jobs_tenant_isolation ON workbuddy_export_jobs USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_export_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_export_artifacts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_export_artifacts_tenant_isolation ON workbuddy_export_artifacts;
CREATE POLICY workbuddy_export_artifacts_tenant_isolation ON workbuddy_export_artifacts USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_export_redeem_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_export_redeem_tokens FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_export_redeem_tokens_tenant_isolation ON workbuddy_export_redeem_tokens;
CREATE POLICY workbuddy_export_redeem_tokens_tenant_isolation ON workbuddy_export_redeem_tokens USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_deletion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_deletion_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_deletion_requests_tenant_isolation ON workbuddy_deletion_requests;
CREATE POLICY workbuddy_deletion_requests_tenant_isolation ON workbuddy_deletion_requests USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_legal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_legal_holds FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_legal_holds_tenant_isolation ON workbuddy_legal_holds;
CREATE POLICY workbuddy_legal_holds_tenant_isolation ON workbuddy_legal_holds USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_archives ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_archives FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_archives_tenant_isolation ON workbuddy_tenant_archives;
CREATE POLICY workbuddy_tenant_archives_tenant_isolation ON workbuddy_tenant_archives USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_deletion_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_deletion_ledger FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_deletion_ledger_tenant_isolation ON workbuddy_deletion_ledger;
CREATE POLICY workbuddy_deletion_ledger_tenant_isolation ON workbuddy_deletion_ledger USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_tombstones ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_tombstones FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_tombstones_tenant_isolation ON workbuddy_tenant_tombstones;
CREATE POLICY workbuddy_tenant_tombstones_tenant_isolation ON workbuddy_tenant_tombstones USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_usage_salts ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_usage_salts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_usage_salts_tenant_isolation ON workbuddy_usage_salts;
CREATE POLICY workbuddy_usage_salts_tenant_isolation ON workbuddy_usage_salts USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_anonymized_usage_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_anonymized_usage_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_anonymized_usage_links_tenant_isolation ON workbuddy_anonymized_usage_links;
CREATE POLICY workbuddy_anonymized_usage_links_tenant_isolation ON workbuddy_anonymized_usage_links USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ──────────────

REVOKE ALL ON TABLE workbuddy_export_jobs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_export_artifacts FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_export_redeem_tokens FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_deletion_requests FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_legal_holds FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_archives FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_deletion_ledger FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_tombstones FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_usage_salts FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_anonymized_usage_links FROM PUBLIC;

UPDATE _schema_version SET version = 22;
