-- Schema v33: WorkBuddy proposal reviewer roster — SQLite fail-closed marker.
--
-- The proposal tables are PostgreSQL only (see 020_workbuddy_proposals.sql): they
-- depend on same-tenant composite keys, FORCE ROW LEVEL SECURITY and the
-- append-only / identity-guard triggers SQLite cannot provide.  This file creates
-- nothing on SQLite on purpose; every proposal entry point runs inside
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError before a single row is read or
-- written, so a SQLite control plane refuses to staff or read a roster instead of
-- pretending to remember one.
--
-- Only the version watermark advances so the upgrade does not repeat.

UPDATE _schema_version SET version = 33;
