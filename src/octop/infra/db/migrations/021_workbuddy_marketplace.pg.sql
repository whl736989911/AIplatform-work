-- Schema v21: WorkBuddy open-platform marketplace — published templates with
-- immutable versions, developer submissions with a freeze/review trail, tenant
-- installations with credential rebinding, consent evidence and explicit
-- upgrades (PostgreSQL only).
--
-- Scope (§4.5 open-platform data model):
--   * marketplace_templates / marketplace_template_versions are the explicitly
--     global public catalogs: they carry no tenant_id and therefore no RLS, and
--     they may only ever hold reviewed, sanitized content (no tenant UUIDs, no
--     personal data, no secret or production credential material);
--   * developer_submissions, submission reviews, installations, credential
--     bindings, consent evidence and upgrade history are tenant rows: every one
--     of them repeats tenant_id, is covered by ENABLE + FORCE ROW LEVEL
--     SECURITY through workbuddy_rls_visible(), and uses same-tenant composite
--     foreign keys into the identity (015), catalog (016) and workflow (017)
--     slices;
--   * a submission freezes its public sanitized content on submit; review
--     decisions are append-only; consents are an append-only ledger that names
--     the accepted template version, license hash, capability hash and time;
--   * an installation copies one fixed template version into the tenant
--     workflow/version path. Templates never rewrite installed history, and an
--     upgrade creates a new local workflow version instead of mutating the old
--     one.
--
-- PostgreSQL only: SQLite platforms apply the marker
-- 021_workbuddy_marketplace.sql and every marketplace entry point fails closed
-- with WORKBUDDY_POSTGRES_REQUIRED through octop.infra.db.workbuddy_context.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on the regex "; \s* \n", so each top-level statement
--     must end its line with a semicolon, and a plpgsql body must never contain
--     a semicolon directly followed by a newline: end such a line with a
--     trailing comment ("; -- ...") so the split cannot land inside the body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- ── Guard triggers (append-only reuse the 015 helpers verbatim) ──────────────

CREATE OR REPLACE FUNCTION workbuddy_marketplace_guard_template_version() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  RAISE EXCEPTION 'workbuddy: marketplace template versions are immutable' USING ERRCODE = '42501'; -- published content can never be rewritten
END $wb$;

CREATE OR REPLACE FUNCTION workbuddy_marketplace_guard_submission() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'workbuddy: submission identity is immutable' USING ERRCODE = '23514'; END IF; -- identity never moves
  IF OLD.status <> 'draft' THEN
    IF NEW.definition IS DISTINCT FROM OLD.definition OR NEW.definition_hash IS DISTINCT FROM OLD.definition_hash OR NEW.license_id IS DISTINCT FROM OLD.license_id OR NEW.license_text_hash IS DISTINCT FROM OLD.license_text_hash OR NEW.requested_capabilities IS DISTINCT FROM OLD.requested_capabilities OR NEW.name IS DISTINCT FROM OLD.name THEN RAISE EXCEPTION 'workbuddy: a submitted recommendation is frozen' USING ERRCODE = '23514'; END IF; -- freeze public content
    IF NEW.frozen_definition IS DISTINCT FROM OLD.frozen_definition OR NEW.frozen_definition_hash IS DISTINCT FROM OLD.frozen_definition_hash THEN RAISE EXCEPTION 'workbuddy: frozen submission content is immutable' USING ERRCODE = '23514'; END IF; -- frozen bytes never change
  END IF; -- draft rows stay editable
  IF OLD.status <> NEW.status THEN
    IF NOT ((OLD.status = 'draft' AND NEW.status = 'submitted') OR (OLD.status = 'submitted' AND NEW.status IN ('approved', 'rejected'))) THEN RAISE EXCEPTION 'workbuddy: illegal submission transition % to %', OLD.status, NEW.status USING ERRCODE = '23514'; END IF; -- declared state machine only
  END IF; -- transitions only, no rewrites
  IF NEW.status = 'submitted' AND (NEW.submitted_at IS NULL OR NEW.frozen_definition IS NULL OR NEW.frozen_definition_hash IS NULL) THEN RAISE EXCEPTION 'workbuddy: submitting requires frozen content' USING ERRCODE = '23514'; END IF; -- freeze before review
  IF NEW.status = 'approved' AND NEW.published_template_version_id IS NULL THEN RAISE EXCEPTION 'workbuddy: approval requires the published template version' USING ERRCODE = '23514'; END IF; -- approval publishes a fixed version
  IF NEW.status <> 'approved' AND NEW.published_template_version_id IS NOT NULL THEN RAISE EXCEPTION 'workbuddy: only an approved submission carries a published version' USING ERRCODE = '23514'; END IF; -- publication pointer stays honest
  RETURN NEW; -- accepted
END $wb$;

CREATE OR REPLACE FUNCTION workbuddy_marketplace_guard_installation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'workbuddy: installation identity is immutable' USING ERRCODE = '23514'; END IF; -- identity never moves
  IF NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.installed_by IS DISTINCT FROM OLD.installed_by THEN RAISE EXCEPTION 'workbuddy: installation origin is immutable' USING ERRCODE = '23514'; END IF; -- install provenance never changes
  IF OLD.workflow_id IS NOT NULL AND NEW.workflow_id IS DISTINCT FROM OLD.workflow_id THEN RAISE EXCEPTION 'workbuddy: the installed workflow binding is immutable' USING ERRCODE = '23514'; END IF; -- upgrades append versions, never rebind
  RETURN NEW; -- accepted
END $wb$;

-- ── Global catalog: reviewed templates and their immutable versions ──────────

CREATE TABLE IF NOT EXISTS marketplace_templates (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug              TEXT NOT NULL,
  name              TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  industry          TEXT NOT NULL DEFAULT '',
  publisher_display TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'published',
  current_version_id UUID,
  origin_tenant_id  UUID,
  origin_submission_id UUID,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_templates_slug_key UNIQUE (slug),
  CONSTRAINT marketplace_templates_status_check CHECK (status IN ('published', 'withdrawn')),
  CONSTRAINT marketplace_templates_slug_check CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,63}$'),
  CONSTRAINT marketplace_templates_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT marketplace_templates_publisher_check CHECK (char_length(btrim(publisher_display)) BETWEEN 1 AND 120),
  CONSTRAINT marketplace_templates_description_check CHECK (char_length(description) <= 2000),
  CONSTRAINT marketplace_templates_industry_check CHECK (char_length(industry) <= 120),
  CONSTRAINT marketplace_templates_published_version_check CHECK (status <> 'published' OR current_version_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_marketplace_templates_status ON marketplace_templates(status, industry, name);

CREATE TABLE IF NOT EXISTS marketplace_template_versions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id         UUID NOT NULL REFERENCES marketplace_templates(id) ON DELETE CASCADE,
  version             TEXT NOT NULL,
  definition          JSONB NOT NULL,
  definition_hash     TEXT NOT NULL,
  schema_version      INTEGER NOT NULL DEFAULT 1,
  license_id          TEXT NOT NULL,
  license_text_hash   TEXT NOT NULL,
  required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  content_summary     TEXT NOT NULL DEFAULT '',
  reviewer_note       TEXT,
  published_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  published_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  origin_tenant_id    UUID,
  origin_submission_id UUID,
  CONSTRAINT marketplace_template_versions_template_version_key UNIQUE (template_id, version),
  CONSTRAINT marketplace_template_versions_template_id_key UNIQUE (template_id, id),
  CONSTRAINT marketplace_template_versions_version_check CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  CONSTRAINT marketplace_template_versions_hash_check CHECK (definition_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_template_versions_license_hash_check CHECK (license_text_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_template_versions_schema_check CHECK (schema_version = 1),
  CONSTRAINT marketplace_template_versions_definition_check CHECK (jsonb_typeof(definition) = 'object'),
  CONSTRAINT marketplace_template_versions_capabilities_check CHECK (jsonb_typeof(required_capabilities) = 'array'),
  CONSTRAINT marketplace_template_versions_summary_check CHECK (char_length(content_summary) <= 1000),
  CONSTRAINT marketplace_template_versions_license_id_check CHECK (license_id ~ '^[a-z0-9][a-z0-9._-]{1,63}$')
);

CREATE INDEX IF NOT EXISTS idx_marketplace_template_versions_template ON marketplace_template_versions(template_id, published_at DESC);

ALTER TABLE marketplace_templates DROP CONSTRAINT IF EXISTS marketplace_templates_current_version_fkey;
ALTER TABLE marketplace_templates ADD CONSTRAINT marketplace_templates_current_version_fkey FOREIGN KEY (id, current_version_id)
  REFERENCES marketplace_template_versions(template_id, id) ON DELETE NO ACTION;

DROP TRIGGER IF EXISTS marketplace_template_versions_immutable ON marketplace_template_versions;
CREATE TRIGGER marketplace_template_versions_immutable BEFORE UPDATE OR DELETE ON marketplace_template_versions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_marketplace_guard_template_version();

DROP TRIGGER IF EXISTS marketplace_template_versions_no_truncate ON marketplace_template_versions;
CREATE TRIGGER marketplace_template_versions_no_truncate BEFORE TRUNCATE ON marketplace_template_versions
  FOR STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Tenant rows: developer submissions and their review trail ────────────────

CREATE TABLE IF NOT EXISTS developer_submissions (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  submitted_by          UUID NOT NULL,
  submitted_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  name                  TEXT NOT NULL,
  summary               TEXT NOT NULL DEFAULT '',
  industry              TEXT NOT NULL DEFAULT '',
  definition            JSONB NOT NULL,
  definition_hash       TEXT NOT NULL,
  license_id            TEXT NOT NULL,
  license_text_hash     TEXT NOT NULL,
  requested_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  status                TEXT NOT NULL DEFAULT 'draft',
  frozen_definition     JSONB,
  frozen_definition_hash TEXT,
  review_note           TEXT,
  platform_review_ref   TEXT,
  published_template_version_id UUID,
  submitted_at          TIMESTAMPTZ,
  reviewed_at           TIMESTAMPTZ,
  reviewed_by_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  revision              BIGINT NOT NULL DEFAULT 1,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT developer_submissions_tenant_id_key UNIQUE (tenant_id, id),
  CONSTRAINT developer_submissions_status_check CHECK (status IN ('draft', 'submitted', 'approved', 'rejected')),
  CONSTRAINT developer_submissions_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT developer_submissions_summary_check CHECK (char_length(summary) <= 1000),
  CONSTRAINT developer_submissions_industry_check CHECK (char_length(industry) <= 120),
  CONSTRAINT developer_submissions_review_note_check CHECK (review_note IS NULL OR char_length(review_note) <= 1000),
  CONSTRAINT developer_submissions_definition_check CHECK (jsonb_typeof(definition) = 'object'),
  CONSTRAINT developer_submissions_hash_check CHECK (definition_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT developer_submissions_license_hash_check CHECK (license_text_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT developer_submissions_capabilities_check CHECK (jsonb_typeof(requested_capabilities) = 'array'),
  CONSTRAINT developer_submissions_frozen_type_check CHECK (frozen_definition IS NULL OR jsonb_typeof(frozen_definition) = 'object'),
  CONSTRAINT developer_submissions_frozen_hash_check CHECK ((frozen_definition IS NULL) = (frozen_definition_hash IS NULL)),
  CONSTRAINT developer_submissions_frozen_hash_format_check CHECK (frozen_definition_hash IS NULL OR frozen_definition_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT developer_submissions_revision_check CHECK (revision >= 1),
  CONSTRAINT developer_submissions_submitted_at_check CHECK ((status = 'draft') = (submitted_at IS NULL)),
  CONSTRAINT developer_submissions_frozen_required_check CHECK (status = 'draft' OR frozen_definition IS NOT NULL),
  CONSTRAINT developer_submissions_published_pointer_check CHECK (published_template_version_id IS NULL OR status = 'approved'),
  CONSTRAINT developer_submissions_author_fkey FOREIGN KEY (tenant_id, submitted_by)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION,
  CONSTRAINT developer_submissions_published_version_fkey FOREIGN KEY (published_template_version_id)
    REFERENCES marketplace_template_versions(id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_developer_submissions_tenant_status ON developer_submissions(tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_developer_submissions_tenant_author ON developer_submissions(tenant_id, submitted_by, updated_at DESC);

DROP TRIGGER IF EXISTS developer_submissions_guard ON developer_submissions;
CREATE TRIGGER developer_submissions_guard BEFORE UPDATE ON developer_submissions
  FOR EACH ROW EXECUTE FUNCTION workbuddy_marketplace_guard_submission();

CREATE TABLE IF NOT EXISTS developer_submission_reviews (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  submission_id       UUID NOT NULL,
  decision            TEXT NOT NULL,
  note                TEXT,
  platform_review_ref TEXT NOT NULL,
  reviewer_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  published_template_version_id UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT developer_submission_reviews_tenant_id_key UNIQUE (tenant_id, id),
  CONSTRAINT developer_submission_reviews_decision_check CHECK (decision IN ('approved', 'rejected')),
  CONSTRAINT developer_submission_reviews_ref_check CHECK (char_length(btrim(platform_review_ref)) BETWEEN 1 AND 200),
  CONSTRAINT developer_submission_reviews_note_check CHECK (note IS NULL OR char_length(note) <= 1000),
  CONSTRAINT developer_submission_reviews_pointer_check CHECK ((decision = 'approved') = (published_template_version_id IS NOT NULL)),
  CONSTRAINT developer_submission_reviews_submission_fkey FOREIGN KEY (tenant_id, submission_id)
    REFERENCES developer_submissions(tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT developer_submission_reviews_version_fkey FOREIGN KEY (published_template_version_id)
    REFERENCES marketplace_template_versions(id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_developer_submission_reviews_submission ON developer_submission_reviews(tenant_id, submission_id, created_at DESC);

DROP TRIGGER IF EXISTS developer_submission_reviews_append_only ON developer_submission_reviews;
CREATE TRIGGER developer_submission_reviews_append_only BEFORE UPDATE OR DELETE ON developer_submission_reviews
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS developer_submission_reviews_no_truncate ON developer_submission_reviews;
CREATE TRIGGER developer_submission_reviews_no_truncate BEFORE TRUNCATE ON developer_submission_reviews
  FOR STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Tenant rows: installations, credential rebinding, consent evidence ───────

CREATE TABLE IF NOT EXISTS marketplace_installations (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  template_id          UUID NOT NULL,
  template_version_id  UUID NOT NULL,
  workflow_id          UUID,
  installed_version_id UUID,
  installed_by         UUID NOT NULL,
  installed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status               TEXT NOT NULL DEFAULT 'pending',
  consented_license_hash    TEXT,
  consented_capabilities    JSONB,
  consented_at         TIMESTAMPTZ,
  job_id               UUID,
  error_code           TEXT,
  error_detail         TEXT,
  revision             BIGINT NOT NULL DEFAULT 1,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_installations_tenant_id_key UNIQUE (tenant_id, id),
  CONSTRAINT marketplace_installations_status_check CHECK (status IN ('pending', 'installing', 'installed', 'failed')),
  CONSTRAINT marketplace_installations_revision_check CHECK (revision >= 1),
  CONSTRAINT marketplace_installations_capabilities_check CHECK (consented_capabilities IS NULL OR jsonb_typeof(consented_capabilities) = 'array'),
  CONSTRAINT marketplace_installations_license_hash_check CHECK (consented_license_hash IS NULL OR consented_license_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_installations_error_check CHECK (char_length(coalesce(error_code, '')) <= 120),
  CONSTRAINT marketplace_installations_detail_check CHECK (error_detail IS NULL OR char_length(error_detail) <= 1000),
  CONSTRAINT marketplace_installations_installed_check CHECK (status <> 'installed' OR (workflow_id IS NOT NULL AND installed_version_id IS NOT NULL)),
  CONSTRAINT marketplace_installations_consent_check CHECK (consented_at IS NULL OR (consented_license_hash IS NOT NULL AND consented_capabilities IS NOT NULL)),
  CONSTRAINT marketplace_installations_installer_fkey FOREIGN KEY (tenant_id, installed_by)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_installations_template_version_fkey FOREIGN KEY (template_id, template_version_id)
    REFERENCES marketplace_template_versions(template_id, id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_installations_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_installations_workflow_version_fkey FOREIGN KEY (tenant_id, workflow_id, installed_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_id, workflow_version_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_marketplace_installations_tenant_status ON marketplace_installations(tenant_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_marketplace_installations_tenant_template
  ON marketplace_installations(tenant_id, template_id) WHERE status <> 'failed';

DROP TRIGGER IF EXISTS marketplace_installations_guard ON marketplace_installations;
CREATE TRIGGER marketplace_installations_guard BEFORE UPDATE ON marketplace_installations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_marketplace_guard_installation();

DROP TRIGGER IF EXISTS marketplace_installations_immutable_id ON marketplace_installations;
CREATE TRIGGER marketplace_installations_immutable_id BEFORE UPDATE ON marketplace_installations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('id');

CREATE TABLE IF NOT EXISTS installation_credential_bindings (
  tenant_id       UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  installation_id UUID NOT NULL,
  binding_key     TEXT NOT NULL,
  credential_id   UUID NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, installation_id, binding_key),
  CONSTRAINT installation_credential_bindings_key_check CHECK (binding_key ~ '^[a-z][a-z0-9_]{0,63}$'),
  CONSTRAINT installation_credential_bindings_installation_fkey FOREIGN KEY (tenant_id, installation_id)
    REFERENCES marketplace_installations(tenant_id, id) ON DELETE NO ACTION,
  CONSTRAINT installation_credential_bindings_credential_fkey FOREIGN KEY (tenant_id, credential_id)
    REFERENCES workbuddy_connector_credentials(tenant_id, credential_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_installation_credential_bindings_credential ON installation_credential_bindings(tenant_id, credential_id);

CREATE TABLE IF NOT EXISTS marketplace_installation_consents (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  installation_id     UUID NOT NULL,
  subject_kind        TEXT NOT NULL,
  subject_id          UUID NOT NULL,
  template_id         UUID NOT NULL,
  template_version_id UUID NOT NULL,
  license_id          TEXT NOT NULL,
  license_text_hash   TEXT NOT NULL,
  capabilities        JSONB NOT NULL,
  capabilities_hash   TEXT NOT NULL,
  consented_by        UUID NOT NULL,
  consented_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  consented_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_installation_consents_tenant_id_key UNIQUE (tenant_id, id),
  CONSTRAINT marketplace_installation_consents_subject_check CHECK (subject_kind IN ('install', 'upgrade')),
  CONSTRAINT marketplace_installation_consents_license_hash_check CHECK (license_text_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_installation_consents_capabilities_hash_check CHECK (capabilities_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_installation_consents_capabilities_check CHECK (jsonb_typeof(capabilities) = 'array'),
  CONSTRAINT marketplace_installation_consents_installation_fkey FOREIGN KEY (tenant_id, installation_id)
    REFERENCES marketplace_installations(tenant_id, id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_installation_consents_version_fkey FOREIGN KEY (template_id, template_version_id)
    REFERENCES marketplace_template_versions(template_id, id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_installation_consents_member_fkey FOREIGN KEY (tenant_id, consented_by)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_marketplace_installation_consents_installation ON marketplace_installation_consents(tenant_id, installation_id, consented_at DESC);

DROP TRIGGER IF EXISTS marketplace_installation_consents_append_only ON marketplace_installation_consents;
CREATE TRIGGER marketplace_installation_consents_append_only BEFORE UPDATE OR DELETE ON marketplace_installation_consents
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS marketplace_installation_consents_no_truncate ON marketplace_installation_consents;
CREATE TRIGGER marketplace_installation_consents_no_truncate BEFORE TRUNCATE ON marketplace_installation_consents
  FOR STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Tenant rows: explicit upgrade history (installed versions are append-only) ─

CREATE TABLE IF NOT EXISTS marketplace_upgrades (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  installation_id      UUID NOT NULL,
  from_template_version_id UUID NOT NULL,
  to_template_version_id   UUID NOT NULL,
  workflow_id          UUID NOT NULL,
  workflow_version_id  UUID,
  requested_by         UUID NOT NULL,
  requested_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status               TEXT NOT NULL DEFAULT 'pending',
  consented_license_hash TEXT,
  consented_capabilities JSONB,
  consented_at         TIMESTAMPTZ,
  job_id               UUID,
  error_code           TEXT,
  error_detail         TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_upgrades_tenant_id_key UNIQUE (tenant_id, id),
  CONSTRAINT marketplace_upgrades_status_check CHECK (status IN ('pending', 'installing', 'installed', 'failed')),
  CONSTRAINT marketplace_upgrades_version_change_check CHECK (to_template_version_id <> from_template_version_id),
  CONSTRAINT marketplace_upgrades_capabilities_check CHECK (consented_capabilities IS NULL OR jsonb_typeof(consented_capabilities) = 'array'),
  CONSTRAINT marketplace_upgrades_license_hash_check CHECK (consented_license_hash IS NULL OR consented_license_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT marketplace_upgrades_consent_check CHECK (consented_at IS NULL OR (consented_license_hash IS NOT NULL AND consented_capabilities IS NOT NULL)),
  CONSTRAINT marketplace_upgrades_installed_check CHECK (status <> 'installed' OR workflow_version_id IS NOT NULL),
  CONSTRAINT marketplace_upgrades_error_check CHECK (char_length(coalesce(error_code, '')) <= 120),
  CONSTRAINT marketplace_upgrades_detail_check CHECK (error_detail IS NULL OR char_length(error_detail) <= 1000),
  CONSTRAINT marketplace_upgrades_installation_fkey FOREIGN KEY (tenant_id, installation_id)
    REFERENCES marketplace_installations(tenant_id, id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_upgrades_from_version_fkey FOREIGN KEY (from_template_version_id)
    REFERENCES marketplace_template_versions(id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_upgrades_to_version_fkey FOREIGN KEY (to_template_version_id)
    REFERENCES marketplace_template_versions(id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_upgrades_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_upgrades_workflow_version_fkey FOREIGN KEY (tenant_id, workflow_id, workflow_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_id, workflow_version_id) ON DELETE NO ACTION,
  CONSTRAINT marketplace_upgrades_requester_fkey FOREIGN KEY (tenant_id, requested_by)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_marketplace_upgrades_installation ON marketplace_upgrades(tenant_id, installation_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_marketplace_upgrades_open
  ON marketplace_upgrades(tenant_id, installation_id) WHERE status IN ('pending', 'installing');

-- ── Row level security: every tenant marketplace row is isolated by app.tenant_id ──

ALTER TABLE developer_submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE developer_submissions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS developer_submissions_tenant_isolation ON developer_submissions;
CREATE POLICY developer_submissions_tenant_isolation ON developer_submissions USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE developer_submission_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE developer_submission_reviews FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS developer_submission_reviews_tenant_isolation ON developer_submission_reviews;
CREATE POLICY developer_submission_reviews_tenant_isolation ON developer_submission_reviews USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE marketplace_installations ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_installations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS marketplace_installations_tenant_isolation ON marketplace_installations;
CREATE POLICY marketplace_installations_tenant_isolation ON marketplace_installations USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE installation_credential_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE installation_credential_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS installation_credential_bindings_tenant_isolation ON installation_credential_bindings;
CREATE POLICY installation_credential_bindings_tenant_isolation ON installation_credential_bindings USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE marketplace_installation_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_installation_consents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS marketplace_installation_consents_tenant_isolation ON marketplace_installation_consents;
CREATE POLICY marketplace_installation_consents_tenant_isolation ON marketplace_installation_consents USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE marketplace_upgrades ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_upgrades FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS marketplace_upgrades_tenant_isolation ON marketplace_upgrades;
CREATE POLICY marketplace_upgrades_tenant_isolation ON marketplace_upgrades USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE marketplace_templates FROM PUBLIC;
REVOKE ALL ON TABLE marketplace_template_versions FROM PUBLIC;
REVOKE ALL ON TABLE developer_submissions FROM PUBLIC;
REVOKE ALL ON TABLE developer_submission_reviews FROM PUBLIC;
REVOKE ALL ON TABLE marketplace_installations FROM PUBLIC;
REVOKE ALL ON TABLE installation_credential_bindings FROM PUBLIC;
REVOKE ALL ON TABLE marketplace_installation_consents FROM PUBLIC;
REVOKE ALL ON TABLE marketplace_upgrades FROM PUBLIC;

REVOKE ALL ON FUNCTION workbuddy_marketplace_guard_template_version() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_marketplace_guard_submission() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_marketplace_guard_installation() FROM PUBLIC;

UPDATE _schema_version SET version = 21;
