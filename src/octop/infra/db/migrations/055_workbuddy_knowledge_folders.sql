-- Schema v55: document folders — SQLite fail-closed marker.
--
-- The WorkBuddy knowledge tables are PostgreSQL only, so the document folder path
-- (and the shape check that keeps it navigable) is applied on PostgreSQL alone.
-- This file creates nothing on SQLite on purpose.
--
-- Every WorkBuddy entry point runs inside
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED) before a
-- single row is read or written, so a SQLite control plane fails closed instead
-- of listing folders it cannot isolate.
--
-- Only the version watermark advances so the upgrade does not repeat and the
-- SQLite control plane stays usable for every non-WorkBuddy feature.

UPDATE _schema_version SET version = 55;
