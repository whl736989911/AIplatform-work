-- Schema v56: local ONNX embedding models as platform model revisions (B-11).
--
-- A local ONNX embedding model is not a second kind of catalog entry: it is a
-- platform model revision whose adapter is ``onnx`` and whose ``model_key`` is
-- the local ONNX model id. Two columns carry the declaration the knowledge pin
-- needs to trust it:
--
-- * ``embedding_dimensions`` — the width the revision produces. Nullable because
--   every pre-existing row (bge-m3 and friends) is covered by the platform
--   constant; an ONNX row has to declare it.
-- * ``local_model_id`` — the downloaded model id under
--   ``~/.octop/embedding_models``. It is *not* a path: the embedder resolves it
--   through the local ONNX catalog, so a raw write cannot point at arbitrary
--   files.
--
-- Dimensions are only accepted at the storage layer's width (1024) today: the
-- embedding column is a fixed-width ``vector(1024)``, so a revision declaring
-- another width could be published but never stored. Supporting other widths
-- needs the vector column (and its migrations) to change first.
--
-- The declaration is frozen with the rest of the revision — the immutability
-- trigger compares the whole row minus status/revocation columns, so a published
-- ONNX revision can only be revoked, never re-pointed at another local model.

ALTER TABLE workbuddy_platform_model_revisions
  ADD COLUMN IF NOT EXISTS embedding_dimensions INTEGER;
ALTER TABLE workbuddy_platform_model_revisions
  ADD COLUMN IF NOT EXISTS local_model_id TEXT;

ALTER TABLE workbuddy_platform_model_revisions
  DROP CONSTRAINT IF EXISTS wb_platform_model_revisions_embedding_dimensions_valid;
ALTER TABLE workbuddy_platform_model_revisions
  ADD CONSTRAINT wb_platform_model_revisions_embedding_dimensions_valid
  CHECK (embedding_dimensions IS NULL OR embedding_dimensions > 0);

ALTER TABLE workbuddy_platform_model_revisions
  DROP CONSTRAINT IF EXISTS wb_platform_model_revisions_local_model_id_valid;
ALTER TABLE workbuddy_platform_model_revisions
  ADD CONSTRAINT wb_platform_model_revisions_local_model_id_valid
  CHECK (local_model_id IS NULL OR char_length(btrim(local_model_id)) BETWEEN 1 AND 200);

-- An ``onnx`` revision without both fields cannot be pinned to a knowledge base
-- (the service refuses it) and cannot be embedded (the hook has nothing to
-- load), so the schema refuses to store that shape at all — including through a
-- raw write that bypasses the repository.
ALTER TABLE workbuddy_platform_model_revisions
  DROP CONSTRAINT IF EXISTS wb_platform_model_revisions_onnx_declaration;
ALTER TABLE workbuddy_platform_model_revisions
  ADD CONSTRAINT wb_platform_model_revisions_onnx_declaration CHECK (
    adapter_key <> 'onnx'
    OR (local_model_id IS NOT NULL AND embedding_dimensions IS NOT NULL)
  );

-- The catalog listing and the admin views read published revisions per adapter
-- family; the existing indexes cover key lookups and the status alone.
CREATE INDEX IF NOT EXISTS wb_platform_model_revisions_adapter_status_idx
  ON workbuddy_platform_model_revisions(adapter_key, status);

UPDATE _schema_version SET version = 56;
