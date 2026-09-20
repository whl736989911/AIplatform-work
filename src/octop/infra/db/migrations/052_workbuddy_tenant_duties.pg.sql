-- Schema v52: WorkBuddy tenant duties (B-05).
--
-- A duty is a job a member may do without being promoted to tenant admin:
-- author (save workflow versions), publisher (publish/roll back/archive),
-- approver (decide approvals), kb_admin (govern knowledge bases), ops (operate
-- runs and platform allowances).
--
-- A row is a subject grant with the same three subject kinds as every other
-- grant in the platform: ``tenant`` (every active member), ``department`` (that
-- department and its sub-departments), or ``member`` (one user id). A tenant
-- admin holds every duty implicitly, so these rows only add a path for members
-- who are not admins.

CREATE TABLE IF NOT EXISTS workbuddy_tenant_duty_grants (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  duty                     TEXT NOT NULL,
  subject_key              TEXT NOT NULL DEFAULT 'tenant',
  user_id                  INTEGER REFERENCES users(id) ON DELETE CASCADE,
  department_id            UUID,
  granted_by_membership_id UUID,
  granted_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, duty, subject_key),
  CONSTRAINT workbuddy_duty_grants_duty_valid CHECK (
    duty IN ('author', 'publisher', 'approver', 'kb_admin', 'ops')
  ),
  CONSTRAINT workbuddy_duty_grants_subject_shape CHECK (
    (subject_key = 'tenant' AND user_id IS NULL AND department_id IS NULL)
    OR (subject_key = 'member:' || user_id::text AND user_id IS NOT NULL AND department_id IS NULL)
    OR (subject_key = 'department:' || department_id::text AND department_id IS NOT NULL AND user_id IS NULL)
  ),
  CONSTRAINT workbuddy_duty_grants_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_duty_grants_member
  ON workbuddy_tenant_duty_grants(tenant_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workbuddy_duty_grants_department
  ON workbuddy_tenant_duty_grants(tenant_id, department_id) WHERE department_id IS NOT NULL;

-- Identity is immutable: a duty row that could be re-pointed at another subject
-- would silently move authority. workbuddy_guard_immutable_column() is defined
-- in 015_workbuddy_identity.pg.sql and takes the column name.
DROP TRIGGER IF EXISTS workbuddy_duty_grants_immutable_tenant ON workbuddy_tenant_duty_grants;
CREATE TRIGGER workbuddy_duty_grants_immutable_tenant BEFORE UPDATE ON workbuddy_tenant_duty_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('tenant_id');
DROP TRIGGER IF EXISTS workbuddy_duty_grants_immutable_duty ON workbuddy_tenant_duty_grants;
CREATE TRIGGER workbuddy_duty_grants_immutable_duty BEFORE UPDATE ON workbuddy_tenant_duty_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('duty');
DROP TRIGGER IF EXISTS workbuddy_duty_grants_immutable_subject ON workbuddy_tenant_duty_grants;
CREATE TRIGGER workbuddy_duty_grants_immutable_subject BEFORE UPDATE ON workbuddy_tenant_duty_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('user_id');
DROP TRIGGER IF EXISTS workbuddy_duty_grants_immutable_department ON workbuddy_tenant_duty_grants;
CREATE TRIGGER workbuddy_duty_grants_immutable_department BEFORE UPDATE ON workbuddy_tenant_duty_grants
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('department_id');

ALTER TABLE workbuddy_tenant_duty_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_tenant_duty_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_tenant_duty_grants;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_tenant_duty_grants USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

REVOKE ALL ON TABLE workbuddy_tenant_duty_grants FROM PUBLIC;

UPDATE _schema_version SET version = 52;
