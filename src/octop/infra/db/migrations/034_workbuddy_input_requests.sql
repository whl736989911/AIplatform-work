-- Schema v34 marker for SQLite control planes.
--
-- Input requests and the ``waiting_input`` state a run occupies while it waits
-- for one exist in PostgreSQL only: SQLite never stores executions, so there is
-- nothing here to wait on and no table to create. Every WorkBuddy entry point
-- goes through octop.infra.db.workbuddy_context.workbuddy_transaction, which
-- raises the controlled WorkBuddyPostgresRequiredError before a single row is
-- read or written, so a SQLite control plane refuses a runtime request instead
-- of answering it from a fact store it does not have. Recording the version
-- keeps the migration chain monotonic for SQLite installs, which keep working
-- for every unaffected Octop feature. The PostgreSQL side of this version is
-- 034_workbuddy_input_requests.pg.sql.

UPDATE _schema_version SET version = 34;
