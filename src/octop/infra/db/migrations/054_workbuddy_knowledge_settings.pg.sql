-- Schema v54: knowledge-base settings and per-member preferences (B-10).
--
-- Two personal-edition capabilities that the tenant tables did not have yet:
--
-- * ``max_documents`` — the per-base cap the personal edition has held since v10.
--   It belongs on the base row because it is a property of the collection, and
--   the ingestion path refuses a new document once the cap is reached.
-- * ``workbuddy_knowledge_preferences`` — "default open" is a *per member*
--   preference in a tenant: the personal edition could keep it on the base row
--   because every base had exactly one owner, but a shared tenant base is opened
--   by many members, each with their own answer.
--
-- The base row's cap is added with a bounded check so a caller cannot raise it
-- beyond what the ingestion path can hold; the preference table is tenant
-- isolated like every other WorkBuddy table and cascades with its base.

ALTER TABLE workbuddy_knowledge_bases
  ADD COLUMN IF NOT EXISTS max_documents INTEGER NOT NULL DEFAULT 100;
ALTER TABLE workbuddy_knowledge_bases
  DROP CONSTRAINT IF EXISTS wb_knowledge_bases_max_documents_valid;
ALTER TABLE workbuddy_knowledge_bases
  ADD CONSTRAINT wb_knowledge_bases_max_documents_valid
  CHECK (max_documents BETWEEN 1 AND 100000);

CREATE TABLE IF NOT EXISTS workbuddy_knowledge_preferences (
  tenant_id    UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kb_id        UUID NOT NULL,
  default_open BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at   BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, user_id, kb_id),
  CONSTRAINT wb_knowledge_preferences_kb_fkey FOREIGN KEY (tenant_id, kb_id)
    REFERENCES workbuddy_knowledge_bases(tenant_id, kb_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wb_knowledge_preferences_open_idx
  ON workbuddy_knowledge_preferences(tenant_id, user_id) WHERE default_open;

ALTER TABLE workbuddy_knowledge_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_knowledge_preferences FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_knowledge_preferences;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_knowledge_preferences
  USING (workbuddy_rls_visible(tenant_id)) WITH CHECK (workbuddy_rls_visible(tenant_id));

REVOKE ALL ON TABLE workbuddy_knowledge_preferences FROM PUBLIC;

UPDATE _schema_version SET version = 54;
