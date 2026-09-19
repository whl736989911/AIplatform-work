-- Schema v51: knowledge documents may exist without a source file (PostgreSQL only).
--
-- Source: docs/plan/two-machine-workstreams.md B-09 ("文本-only 迁移通道（放宽
-- file_ref_id 或占位）"). The personal edition's knowledge bases were indexed from
-- text alone — its ``index.sqlite`` holds chunk text and vectors and no original
-- file — so a migrated document cannot name a ``workbuddy_knowledge_file_refs``
-- row. The plan allows either a placeholder file reference or a relaxed column;
-- this migration relaxes the column, because a synthetic "file" would make the
-- file reference describe something that never existed, and the unique index
-- (one document per file per base) would then also forbid a second migrated
-- document in the same base.
--
-- ``source`` is the discriminator and carries the whole rule:
--
--   * 'upload'    — the original: file_ref_id is NOT NULL (unchanged behaviour);
--   * 'text'      — a document created from text supplied by a caller;
--   * 'migration' — a document imported from the personal edition, text only.
--
-- The three values are declared now although this batch only imports 'migration'
-- and 'text', because B-10 adds the text-document capability and re-using the
-- vocabulary avoids a second migration of the same table.
--
-- PostgreSQL only; the SQLite marker records the version and creates nothing
-- (see 019_workbuddy_knowledge_triggers.sql for the fail-closed rationale).

ALTER TABLE workbuddy_knowledge_documents
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'upload';

ALTER TABLE workbuddy_knowledge_documents
  ALTER COLUMN file_ref_id DROP NOT NULL;

-- One document per stored file stays true, but only for documents that have one.
ALTER TABLE workbuddy_knowledge_documents
  DROP CONSTRAINT IF EXISTS wb_knowledge_documents_file_ref_unique;
CREATE UNIQUE INDEX IF NOT EXISTS wb_knowledge_documents_file_ref_unique
  ON workbuddy_knowledge_documents(tenant_id, kb_id, file_ref_id)
  WHERE file_ref_id IS NOT NULL;

ALTER TABLE workbuddy_knowledge_documents
  DROP CONSTRAINT IF EXISTS wb_knowledge_documents_source_valid;
ALTER TABLE workbuddy_knowledge_documents
  ADD CONSTRAINT wb_knowledge_documents_source_valid
    CHECK (source IN ('upload', 'text', 'migration'));

-- An uploaded document must point at its file reference; a text-only document
-- must not pretend to have one.
ALTER TABLE workbuddy_knowledge_documents
  DROP CONSTRAINT IF EXISTS wb_knowledge_documents_source_shape;
ALTER TABLE workbuddy_knowledge_documents
  ADD CONSTRAINT wb_knowledge_documents_source_shape
    CHECK (
      (source = 'upload' AND file_ref_id IS NOT NULL)
      OR (source <> 'upload' AND file_ref_id IS NULL)
    );

UPDATE _schema_version SET version = 51;
