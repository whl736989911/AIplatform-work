-- Schema v22: WorkBuddy tenant lifecycle — SQLite fail-closed marker.
--
-- The lifecycle tables (export jobs/artifacts, redeem tokens, deletion requests,
-- legal holds, archives, deletion ledger, tombstones, anonymized usage linkage)
-- are PostgreSQL only. This file creates nothing on SQLite on purpose: tenant
-- export, redeem, deletion and restore paths all run through
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED)
-- before a single row is read or written. Deletion therefore fails closed on
-- SQLite instead of pretending to queue a purge against a database that cannot
-- enforce RLS or hold the append-only ledger.
--
-- Only the version watermark advances so the upgrade does not repeat.

UPDATE _schema_version SET version = 22;
