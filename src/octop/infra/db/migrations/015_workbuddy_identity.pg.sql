-- Schema v15: WorkBuddy tenancy — tenants, departments, members, invitations,
-- quotas and the tenant governance audit trail.
--
-- PostgreSQL only. SQLite installs apply the no-op marker
-- 015_workbuddy_identity.sql and octop.infra.db.workbuddy_context refuses to
-- serve WorkBuddy with a controlled WORKBUDDY_POSTGRES_REQUIRED error.
-- Requires PostgreSQL 13+ (gen_random_uuid is core since 13).
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on the regex "; \s* \n", so each top-level statement
--     must end its line with a semicolon, and a plpgsql body must never contain a
--     semicolon directly followed by a newline: end such a line with a trailing
--     comment ("; -- ...") so the split cannot land inside the function body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- ── Context helpers (fixed search_path; used by RLS policies and triggers) ────

CREATE OR REPLACE FUNCTION workbuddy_system_context() RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog AS $wb$ SELECT coalesce(current_setting('app.system', true), 'off') = 'on' $wb$;

CREATE OR REPLACE FUNCTION workbuddy_current_tenant_id() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $wb$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $wb$;

CREATE OR REPLACE FUNCTION workbuddy_rls_visible(row_tenant_id uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $wb$ SELECT coalesce(row_tenant_id IS NOT NULL AND (workbuddy_system_context() OR row_tenant_id = workbuddy_current_tenant_id()), false) $wb$;

CREATE OR REPLACE FUNCTION workbuddy_department_max_depth() RETURNS integer LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $wb$ SELECT 8 $wb$;

-- ── Guard triggers ───────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION workbuddy_guard_immutable_column() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  IF to_jsonb(NEW) ->> TG_ARGV[0] IS DISTINCT FROM to_jsonb(OLD) ->> TG_ARGV[0] THEN RAISE EXCEPTION 'workbuddy: column % is immutable', TG_ARGV[0] USING ERRCODE = '23514'; END IF; -- public ids never change
  RETURN NEW; -- accepted
END $wb$;

CREATE OR REPLACE FUNCTION workbuddy_guard_append_only() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
BEGIN
  RAISE EXCEPTION 'workbuddy: % is append only', TG_TABLE_NAME USING ERRCODE = '42501'; -- governance trail is immutable
END $wb$;

CREATE OR REPLACE FUNCTION workbuddy_guard_quota_limit() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
DECLARE cap bigint; -- platform ceiling for the metric
BEGIN
  SELECT hard_cap INTO cap FROM workbuddy_quota_metrics WHERE metric = NEW.metric; -- ceiling row
  IF cap IS NULL THEN RAISE EXCEPTION 'workbuddy: unknown quota metric %', NEW.metric USING ERRCODE = '23514'; END IF; -- unknown metric
  IF NEW.limit_value > cap THEN RAISE EXCEPTION 'workbuddy: quota exceeds hard cap' USING ERRCODE = '23514'; END IF; -- admin hard cap
  RETURN NEW; -- accepted
END $wb$;

CREATE OR REPLACE FUNCTION workbuddy_guard_department_parent() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$
DECLARE ancestor uuid; depth integer := 0; -- climb cursor and counted levels
BEGIN
  IF NEW.parent_department_id IS NULL THEN RETURN NEW; END IF; -- root departments need no walk
  IF NEW.parent_department_id = NEW.department_id THEN RAISE EXCEPTION 'workbuddy: department cycle detected' USING ERRCODE = '23514'; END IF; -- self parent
  ancestor := NEW.parent_department_id; -- start the climb
  WHILE ancestor IS NOT NULL AND depth < 64 LOOP
    IF ancestor = NEW.department_id THEN RAISE EXCEPTION 'workbuddy: department cycle detected' USING ERRCODE = '23514'; END IF; -- revisited row
    SELECT parent_department_id INTO ancestor FROM workbuddy_departments WHERE department_id = ancestor AND tenant_id = NEW.tenant_id; -- climb one level
    depth := depth + 1; -- count the level
  END LOOP; -- end climb
  IF depth + 1 > workbuddy_department_max_depth() THEN RAISE EXCEPTION 'workbuddy: department depth exceeded' USING ERRCODE = '23514'; END IF; -- nesting cap
  RETURN NEW; -- accepted
END $wb$;

-- ── Tenants ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_tenants (
  tenant_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug              TEXT NOT NULL,
  slug_normalized   TEXT NOT NULL,
  name              TEXT NOT NULL,
  plan              TEXT NOT NULL DEFAULT 'standard',
  data_region       TEXT NOT NULL DEFAULT 'cn',
  status            TEXT NOT NULL DEFAULT 'active',
  status_reason     TEXT,
  status_changed_at BIGINT,
  status_changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at        BIGINT NOT NULL,
  updated_at        BIGINT NOT NULL,
  CONSTRAINT workbuddy_tenants_slug_key UNIQUE (slug_normalized),
  CONSTRAINT workbuddy_tenants_status_check CHECK (status IN ('active', 'suspended')),
  CONSTRAINT workbuddy_tenants_slug_normalized_check CHECK (slug_normalized = lower(slug_normalized)),
  CONSTRAINT workbuddy_tenants_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT workbuddy_tenants_plan_check CHECK (char_length(btrim(plan)) BETWEEN 1 AND 40),
  CONSTRAINT workbuddy_tenants_region_check CHECK (char_length(btrim(data_region)) BETWEEN 1 AND 40)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_tenants_status ON workbuddy_tenants(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_tenants_created_at ON workbuddy_tenants(created_at DESC);

-- ── Departments (parent/child inside one tenant, cycle and depth guarded) ────

CREATE TABLE IF NOT EXISTS workbuddy_departments (
  department_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  parent_department_id UUID,
  name                 TEXT NOT NULL,
  name_normalized      TEXT NOT NULL,
  description          TEXT,
  manager_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status               TEXT NOT NULL DEFAULT 'active',
  created_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at           BIGINT NOT NULL,
  updated_at           BIGINT NOT NULL,
  CONSTRAINT workbuddy_departments_name_key UNIQUE (tenant_id, name_normalized),
  CONSTRAINT workbuddy_departments_tenant_id_key UNIQUE (tenant_id, department_id),
  CONSTRAINT workbuddy_departments_status_check CHECK (status IN ('active', 'archived')),
  CONSTRAINT workbuddy_departments_name_normalized_check CHECK (name_normalized = lower(name_normalized)),
  CONSTRAINT workbuddy_departments_name_check CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
  CONSTRAINT workbuddy_departments_self_parent_check CHECK (parent_department_id IS DISTINCT FROM department_id),
  CONSTRAINT workbuddy_departments_parent_fkey FOREIGN KEY (tenant_id, parent_department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_departments_tenant_status ON workbuddy_departments(tenant_id, status, name_normalized);
CREATE INDEX IF NOT EXISTS idx_workbuddy_departments_tenant_parent ON workbuddy_departments(tenant_id, parent_department_id);

-- ── Tenant memberships (link tenant roles to existing Octop users) ───────────

CREATE TABLE IF NOT EXISTS workbuddy_tenant_members (
  membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role          TEXT NOT NULL DEFAULT 'member',
  department_id UUID,
  display_name  TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  invited_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  joined_at     BIGINT NOT NULL,
  updated_at    BIGINT NOT NULL,
  CONSTRAINT workbuddy_members_tenant_user_key UNIQUE (tenant_id, user_id),
  CONSTRAINT workbuddy_members_tenant_id_key UNIQUE (tenant_id, membership_id),
  CONSTRAINT workbuddy_members_role_check CHECK (role IN ('owner', 'admin', 'member')),
  CONSTRAINT workbuddy_members_status_check CHECK (status IN ('active', 'suspended')),
  CONSTRAINT workbuddy_members_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_members_tenant_status ON workbuddy_tenant_members(tenant_id, status, joined_at);
CREATE INDEX IF NOT EXISTS idx_workbuddy_members_tenant_department ON workbuddy_tenant_members(tenant_id, department_id);
CREATE INDEX IF NOT EXISTS idx_workbuddy_members_user ON workbuddy_tenant_members(user_id, joined_at);

-- ── Invitations (email bound; only the sha256 of the token is stored) ────────

CREATE TABLE IF NOT EXISTS workbuddy_invitations (
  invitation_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  email               TEXT NOT NULL,
  email_normalized    TEXT NOT NULL,
  role                TEXT NOT NULL DEFAULT 'member',
  department_id       UUID,
  token_hash          TEXT NOT NULL,
  invited_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  expires_at          BIGINT NOT NULL,
  created_at          BIGINT NOT NULL,
  revoked_at          BIGINT,
  revoked_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
  accepted_at         BIGINT,
  accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT workbuddy_invitations_token_hash_key UNIQUE (token_hash),
  CONSTRAINT workbuddy_invitations_tenant_id_key UNIQUE (tenant_id, invitation_id),
  CONSTRAINT workbuddy_invitations_role_check CHECK (role IN ('owner', 'admin', 'member')),
  CONSTRAINT workbuddy_invitations_email_normalized_check CHECK (email_normalized = lower(email_normalized)),
  CONSTRAINT workbuddy_invitations_email_check CHECK (email_normalized <> ''),
  CONSTRAINT workbuddy_invitations_expiry_check CHECK (expires_at > created_at),
  CONSTRAINT workbuddy_invitations_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_invitations_tenant_created ON workbuddy_invitations(tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workbuddy_invitations_pending_email
  ON workbuddy_invitations(tenant_id, email_normalized)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workbuddy_invitations_pending_expiry
  ON workbuddy_invitations(expires_at)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- ── Quotas (platform ceilings in the metrics table, per-tenant rows below) ───

CREATE TABLE IF NOT EXISTS workbuddy_quota_metrics (
  metric        TEXT PRIMARY KEY,
  unit          TEXT NOT NULL,
  default_limit BIGINT NOT NULL,
  hard_cap      BIGINT NOT NULL,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT workbuddy_quota_metrics_default_check CHECK (default_limit >= 0),
  CONSTRAINT workbuddy_quota_metrics_cap_check CHECK (hard_cap >= 0),
  CONSTRAINT workbuddy_quota_metrics_ceiling_check CHECK (default_limit <= hard_cap)
);

CREATE TABLE IF NOT EXISTS workbuddy_tenant_quotas (
  quota_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  metric      TEXT NOT NULL REFERENCES workbuddy_quota_metrics(metric) ON DELETE NO ACTION,
  limit_value BIGINT NOT NULL,
  updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at  BIGINT NOT NULL,
  updated_at  BIGINT NOT NULL,
  CONSTRAINT workbuddy_tenant_quotas_metric_key UNIQUE (tenant_id, metric),
  CONSTRAINT workbuddy_tenant_quotas_id_key UNIQUE (tenant_id, quota_id),
  CONSTRAINT workbuddy_tenant_quotas_limit_check CHECK (limit_value >= 0)
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_tenant_quotas_tenant ON workbuddy_tenant_quotas(tenant_id, metric);

INSERT INTO workbuddy_quota_metrics(metric, unit, default_limit, hard_cap, sort_order) VALUES
  ('users', 'seats', 25, 500, 10),
  ('departments', 'departments', 10, 100, 20),
  ('agents', 'agents', 10, 200, 30),
  ('connectors', 'connectors', 10, 200, 40),
  ('storage_mb', 'megabytes', 2048, 1048576, 50),
  ('monthly_tokens', 'tokens', 1000000, 100000000, 60)
ON CONFLICT (metric) DO NOTHING;

-- ── Governance audit trail (append only) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_tenant_audit_events (
  event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  action        TEXT NOT NULL,
  actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  reason        TEXT,
  detail        TEXT,
  created_at    BIGINT NOT NULL,
  CONSTRAINT workbuddy_tenant_audit_events_tenant_id_key UNIQUE (tenant_id, event_id),
  CONSTRAINT workbuddy_tenant_audit_events_action_check CHECK (action <> '')
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_tenant_audit_events_tenant ON workbuddy_tenant_audit_events(tenant_id, created_at DESC);

-- ── Triggers: immutability, append-only trail, quota ceiling, cycles ─────────

DROP TRIGGER IF EXISTS workbuddy_tenants_immutable_id ON workbuddy_tenants;
CREATE TRIGGER workbuddy_tenants_immutable_id BEFORE UPDATE ON workbuddy_tenants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('tenant_id');

DROP TRIGGER IF EXISTS workbuddy_departments_immutable_id ON workbuddy_departments;
CREATE TRIGGER workbuddy_departments_immutable_id BEFORE UPDATE ON workbuddy_departments
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('department_id');

DROP TRIGGER IF EXISTS workbuddy_departments_parent_guard ON workbuddy_departments;
CREATE TRIGGER workbuddy_departments_parent_guard BEFORE INSERT OR UPDATE ON workbuddy_departments
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_department_parent();

DROP TRIGGER IF EXISTS workbuddy_members_immutable_id ON workbuddy_tenant_members;
CREATE TRIGGER workbuddy_members_immutable_id BEFORE UPDATE ON workbuddy_tenant_members
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('membership_id');

DROP TRIGGER IF EXISTS workbuddy_invitations_immutable_id ON workbuddy_invitations;
CREATE TRIGGER workbuddy_invitations_immutable_id BEFORE UPDATE ON workbuddy_invitations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('invitation_id');

DROP TRIGGER IF EXISTS workbuddy_tenant_quotas_immutable_id ON workbuddy_tenant_quotas;
CREATE TRIGGER workbuddy_tenant_quotas_immutable_id BEFORE UPDATE ON workbuddy_tenant_quotas
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('quota_id');

DROP TRIGGER IF EXISTS workbuddy_tenant_quotas_cap_guard ON workbuddy_tenant_quotas;
CREATE TRIGGER workbuddy_tenant_quotas_cap_guard BEFORE INSERT OR UPDATE ON workbuddy_tenant_quotas
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_quota_limit();

DROP TRIGGER IF EXISTS workbuddy_tenant_audit_events_append_only ON workbuddy_tenant_audit_events;
CREATE TRIGGER workbuddy_tenant_audit_events_append_only BEFORE UPDATE OR DELETE ON workbuddy_tenant_audit_events
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_tenant_audit_events_no_truncate ON workbuddy_tenant_audit_events;
CREATE TRIGGER workbuddy_tenant_audit_events_no_truncate BEFORE TRUNCATE ON workbuddy_tenant_audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Row level security: every tenant table is isolated by app.tenant_id ──────

ALTER TABLE workbuddy_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenants_tenant_isolation ON workbuddy_tenants;
CREATE POLICY workbuddy_tenants_tenant_isolation ON workbuddy_tenants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_departments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_departments_tenant_isolation ON workbuddy_departments;
CREATE POLICY workbuddy_departments_tenant_isolation ON workbuddy_departments USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_members_tenant_isolation ON workbuddy_tenant_members;
CREATE POLICY workbuddy_members_tenant_isolation ON workbuddy_tenant_members USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_invitations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_invitations_tenant_isolation ON workbuddy_invitations;
CREATE POLICY workbuddy_invitations_tenant_isolation ON workbuddy_invitations USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_quotas ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_quotas FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_quotas_tenant_isolation ON workbuddy_tenant_quotas;
CREATE POLICY workbuddy_tenant_quotas_tenant_isolation ON workbuddy_tenant_quotas USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_tenant_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_audit_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_audit_events_tenant_isolation ON workbuddy_tenant_audit_events;
CREATE POLICY workbuddy_tenant_audit_events_tenant_isolation ON workbuddy_tenant_audit_events USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_tenants FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_departments FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_members FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_invitations FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_quotas FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_quota_metrics FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_tenant_audit_events FROM PUBLIC;

REVOKE ALL ON FUNCTION workbuddy_system_context() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_current_tenant_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_rls_visible(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_department_max_depth() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_guard_immutable_column() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_guard_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_guard_quota_limit() FROM PUBLIC;
REVOKE ALL ON FUNCTION workbuddy_guard_department_parent() FROM PUBLIC;

UPDATE _schema_version SET version = 15;
