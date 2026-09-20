-- Schema v55: document folders (B-10).
--
-- The personal edition modelled a folder as a placeholder document that carries a
-- path, so a folder existed as soon as it held one entry. The tenant tables take
-- the path itself: a document names the folder it lives in, and the folder list
-- is the distinct set of those paths. That keeps one source of truth (the
-- documents) and no second lifecycle to keep in sync.
--
-- The shape check refuses what the service also refuses — an absolute path, a
-- traversal segment, a backslash, a doubled or trailing separator — so a raw
-- write cannot create a path the listing would have to interpret.

ALTER TABLE workbuddy_knowledge_documents
  ADD COLUMN IF NOT EXISTS folder_path TEXT NOT NULL DEFAULT '';
ALTER TABLE workbuddy_knowledge_documents
  DROP CONSTRAINT IF EXISTS wb_knowledge_documents_folder_path_valid;
ALTER TABLE workbuddy_knowledge_documents
  ADD CONSTRAINT wb_knowledge_documents_folder_path_valid CHECK (
    folder_path = ''
    OR (
      folder_path !~ '^/'
      AND folder_path !~ '/$'
      AND folder_path !~ '//'
      AND folder_path !~ '\.\.'
      AND folder_path !~ '(^|/)\.(/|$)'
      AND folder_path !~ '\\'
      AND folder_path !~ '^[[:space:]]'
      AND folder_path !~ '[[:space:]]$'
    )
  );

CREATE INDEX IF NOT EXISTS wb_knowledge_documents_folder_idx
  ON workbuddy_knowledge_documents(tenant_id, kb_id, folder_path, title)
  WHERE deleted_at IS NULL;

UPDATE _schema_version SET version = 55;
