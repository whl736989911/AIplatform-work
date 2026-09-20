-- Schema v35 marker for SQLite control planes.
--
-- Output reviews exist in PostgreSQL only: SQLite never stores executions, so
-- there is no settled run here to review, no produced snapshot to digest and no
-- corrected value to attach. Every WorkBuddy entry point goes through
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError before a single row is read or
-- written, so a SQLite control plane refuses a runtime request instead of
-- answering it from a fact store it does not have. Recording the version keeps
-- the migration chain monotonic for SQLite installs, which keep working for
-- every unaffected Octop feature. The PostgreSQL side of this version is
-- 035_workbuddy_output_reviews.pg.sql.

UPDATE _schema_version SET version = 35;
