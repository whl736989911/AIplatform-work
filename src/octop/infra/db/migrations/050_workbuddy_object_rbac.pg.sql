-- Schema v50: generic WorkBuddy object permissions — scope + ACL (PostgreSQL only).
--
-- Source: docs/plan/two-machine-workstreams.md B-01 ("权限骨架：通用 scope + ACL
-- 表与解析（照抄 KB 模型）"). Purpose: give *any* tenant object the four-layer
-- permission model the product requires — company / department / personal /
-- explicit grant — without adding columns to every resource table.
--
-- The model is the knowledge base's, generalised to any ``object_kind``:
--
--   * workbuddy_object_scopes — one row per permission-bearing object. ``scope``
--     is the implicit layer: personal (owner only), department (its current
--     members) or enterprise (every active tenant member). The shape constraint
--     is the KB one, verbatim: a scope names exactly the subject it implies.
--   * workbuddy_object_acl — the explicit layer ("单独授权"). Additive rows for
--     one user *or* one department. Resolution takes the maximum of the implicit
--     rank and every matching grant, so a grant can never take permission away
--     (see octop.infra.rbac.resolver).
--
-- PostgreSQL only, like every other WorkBuddy tenant table: the isolation below
-- is FORCE ROW LEVEL SECURITY plus composite same-tenant foreign keys, which
-- SQLite cannot provide. The SQLite marker file records the version and creates
-- nothing, so octop.infra.db.workbuddy_context.workbuddy_transaction raises the
-- controlled WORKBUDDY_POSTGRES_REQUIRED before a single row is read or written.
--
-- No bare question marks may appear in this file (the connection proxy rewrites
-- them into psycopg placeholders), and every statement is followed by a newline.

CREATE TABLE IF NOT EXISTS workbuddy_object_scopes (
  tenant_id          UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  object_kind        TEXT NOT NULL,
  object_id          UUID NOT NULL,
  scope              TEXT NOT NULL,
  owner_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
  department_id      UUID,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at         BIGINT NOT NULL,
  updated_at         BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, object_kind, object_id),
  CONSTRAINT wb_object_scopes_kind_valid CHECK (char_length(btrim(object_kind)) BETWEEN 1 AND 64),
  CONSTRAINT wb_object_scopes_scope_valid CHECK (scope IN ('personal', 'department', 'enterprise')),
  CONSTRAINT wb_object_scopes_scope_shape CHECK (
    (scope = 'personal' AND owner_user_id IS NOT NULL AND department_id IS NULL)
    OR (scope = 'department' AND department_id IS NOT NULL AND owner_user_id IS NULL)
    OR (scope = 'enterprise' AND owner_user_id IS NULL AND department_id IS NULL)
  ),
  CONSTRAINT wb_object_scopes_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS wb_object_scopes_tenant_kind_idx
  ON workbuddy_object_scopes(tenant_id, object_kind, scope, created_at DESC);
CREATE INDEX IF NOT EXISTS wb_object_scopes_owner_idx
  ON workbuddy_object_scopes(tenant_id, object_kind, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wb_object_scopes_department_idx
  ON workbuddy_object_scopes(tenant_id, object_kind, department_id) WHERE department_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS workbuddy_object_acl (
  tenant_id          UUID NOT NULL,
  acl_id             UUID NOT NULL DEFAULT gen_random_uuid(),
  object_kind        TEXT NOT NULL,
  object_id          UUID NOT NULL,
  user_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
  department_id      UUID,
  permission         TEXT NOT NULL,
  granted_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at         BIGINT NOT NULL,
  updated_at         BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, acl_id),
  CONSTRAINT wb_object_acl_permission_valid CHECK (permission IN ('read', 'write', 'admin')),
  CONSTRAINT wb_object_acl_subject_exactly_one CHECK ((user_id IS NOT NULL) <> (department_id IS NOT NULL)),
  CONSTRAINT wb_object_acl_subject_fkey FOREIGN KEY (tenant_id, object_kind, object_id)
    REFERENCES workbuddy_object_scopes(tenant_id, object_kind, object_id) ON DELETE CASCADE,
  CONSTRAINT wb_object_acl_department_fkey FOREIGN KEY (tenant_id, department_id)
    REFERENCES workbuddy_departments(tenant_id, department_id) ON DELETE CASCADE
);

-- One live grant per subject and object: re-granting is an update, not a second row.
CREATE UNIQUE INDEX IF NOT EXISTS wb_object_acl_user_idx
  ON workbuddy_object_acl(tenant_id, object_kind, object_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS wb_object_acl_department_idx
  ON workbuddy_object_acl(tenant_id, object_kind, object_id, department_id) WHERE department_id IS NOT NULL;

-- Identity is immutable: a permission row that could be re-pointed at another
-- object would silently move access. workbuddy_guard_immutable_column() is
-- defined in 015_workbuddy_identity.pg.sql and takes the column name.
DROP TRIGGER IF EXISTS wb_object_scopes_immutable_object ON workbuddy_object_scopes;
CREATE TRIGGER wb_object_scopes_immutable_object BEFORE UPDATE ON workbuddy_object_scopes
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('object_id');
DROP TRIGGER IF EXISTS wb_object_scopes_immutable_kind ON workbuddy_object_scopes;
CREATE TRIGGER wb_object_scopes_immutable_kind BEFORE UPDATE ON workbuddy_object_scopes
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('object_kind');
DROP TRIGGER IF EXISTS wb_object_scopes_immutable_tenant ON workbuddy_object_scopes;
CREATE TRIGGER wb_object_scopes_immutable_tenant BEFORE UPDATE ON workbuddy_object_scopes
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('tenant_id');
DROP TRIGGER IF EXISTS wb_object_acl_immutable_object ON workbuddy_object_acl;
CREATE TRIGGER wb_object_acl_immutable_object BEFORE UPDATE ON workbuddy_object_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('object_id');
DROP TRIGGER IF EXISTS wb_object_acl_immutable_kind ON workbuddy_object_acl;
CREATE TRIGGER wb_object_acl_immutable_kind BEFORE UPDATE ON workbuddy_object_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('object_kind');
DROP TRIGGER IF EXISTS wb_object_acl_immutable_tenant ON workbuddy_object_acl;
CREATE TRIGGER wb_object_acl_immutable_tenant BEFORE UPDATE ON workbuddy_object_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('tenant_id');
DROP TRIGGER IF EXISTS wb_object_acl_immutable_subject ON workbuddy_object_acl;
CREATE TRIGGER wb_object_acl_immutable_subject BEFORE UPDATE ON workbuddy_object_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('user_id');
DROP TRIGGER IF EXISTS wb_object_acl_immutable_department ON workbuddy_object_acl;
CREATE TRIGGER wb_object_acl_immutable_department BEFORE UPDATE ON workbuddy_object_acl
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('department_id');

ALTER TABLE workbuddy_object_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_object_scopes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_object_scopes;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_object_scopes USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_object_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_object_acl FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_object_acl;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_object_acl USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

REVOKE ALL ON TABLE workbuddy_object_scopes FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_object_acl FROM PUBLIC;

UPDATE _schema_version SET version = 50;
