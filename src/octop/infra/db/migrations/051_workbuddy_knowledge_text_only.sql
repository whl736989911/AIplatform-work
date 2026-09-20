-- Schema v51: knowledge documents without a source file — SQLite fail-closed marker.
--
-- The WorkBuddy knowledge tables are PostgreSQL only, so the relaxation of
-- ``file_ref_id`` and the ``source`` discriminator it belongs to are applied on
-- PostgreSQL alone. This file creates nothing on SQLite on purpose.
--
-- Every knowledge entry point runs inside
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED) before a
-- single row is read or written, so a SQLite control plane fails closed instead
-- of importing documents into tables it cannot isolate.
--
-- Only the version watermark advances so the upgrade does not repeat and the
-- SQLite control plane stays usable for every non-WorkBuddy feature.

UPDATE _schema_version SET version = 51;
