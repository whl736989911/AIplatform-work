-- Schema v53: default visibility backfill (B-06).
--
-- Before B-02 a workflow had no permission row, and the list query treated that
-- state as company visible. B-02 keeps that fallback for rows it reads, but an
-- object without a row is invisible to the permission model itself — a publisher
-- or an administrator reading through the resolver sees nothing to manage.
-- Going live must not make existing work disappear, so every pre-existing
-- workflow gets the row it implicitly had: ``enterprise``.
--
-- The statement is idempotent: it only inserts where no row exists, and the
-- primary key makes a re-run a no-op. Installations whose recorded version
-- already skipped this file are repaired by
-- ``octop.infra.db.migrate._ensure_workbuddy_visibility_backfill``.

INSERT INTO workbuddy_object_scopes (
  tenant_id, object_kind, object_id, scope, owner_user_id, department_id,
  created_by_user_id, created_at, updated_at
)
SELECT
  w.tenant_id, 'workflow', w.workflow_id, 'enterprise', NULL, NULL,
  w.created_by, w.created_at, w.updated_at
FROM workbuddy_workflows w
WHERE NOT EXISTS (
  SELECT 1 FROM workbuddy_object_scopes s
  WHERE s.tenant_id = w.tenant_id
    AND s.object_kind = 'workflow'
    AND s.object_id = w.workflow_id
)
ON CONFLICT (tenant_id, object_kind, object_id) DO NOTHING;

UPDATE _schema_version SET version = 53;
