-- Schema v21: WorkBuddy marketplace — SQLite fail-closed marker.
--
-- The marketplace tables (published templates with immutable versions,
-- developer submissions, submission reviews, installations, credential
-- bindings, consent evidence and upgrade history) are PostgreSQL only: they
-- depend on same-tenant composite foreign keys, FORCE ROW LEVEL SECURITY and
-- append-only triggers that SQLite cannot provide. This file therefore creates
-- nothing on SQLite on purpose.
--
-- Every marketplace entry point runs inside
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED) before
-- a single row is read or written, so a SQLite control plane fails closed
-- instead of installing a template into a database that cannot enforce tenant
-- isolation or freeze published content.
--
-- Only the version watermark advances so the upgrade does not repeat and the
-- SQLite control plane stays usable for every non-WorkBuddy feature.

UPDATE _schema_version SET version = 21;
