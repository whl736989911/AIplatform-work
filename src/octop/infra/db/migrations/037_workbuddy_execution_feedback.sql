-- Schema v37 marker for SQLite control planes.
--
-- Execution feedback exists in PostgreSQL only: SQLite never stores executions,
-- so there is no run here whose output a person could correct and no value a
-- person supplied that could be captured against one. Every WorkBuddy entry
-- point goes through octop.infra.db.workbuddy_context.workbuddy_transaction,
-- which raises the controlled WorkBuddyPostgresRequiredError before a single row
-- is read or written, so a SQLite control plane refuses a runtime request
-- instead of answering it from a fact store it does not have. Recording the
-- version keeps the migration chain monotonic for SQLite installs, which keep
-- working for every unaffected Octop feature. The PostgreSQL side of this
-- version is 037_workbuddy_execution_feedback.pg.sql.

UPDATE _schema_version SET version = 37;
