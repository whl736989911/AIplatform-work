-- Schema v50: generic WorkBuddy object permissions — SQLite fail-closed marker.
--
-- The permission tables (implicit scope per object, explicit additive grants) are
-- PostgreSQL only: they depend on same-tenant composite foreign keys, FORCE ROW
-- LEVEL SECURITY and an immutable-identity trigger that SQLite cannot provide.
-- This file therefore creates nothing on SQLite on purpose.
--
-- Every permission entry point runs inside
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED before a
-- single row is read or written), so a SQLite control plane fails closed instead
-- of serving tenant objects whose access rows it cannot isolate.
--
-- Only the version watermark advances so the upgrade does not repeat and the
-- SQLite control plane stays usable for every non-WorkBuddy feature.

UPDATE _schema_version SET version = 50;
