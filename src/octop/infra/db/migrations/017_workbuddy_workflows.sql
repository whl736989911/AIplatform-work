-- WorkBuddy workflow definitions are PostgreSQL-only: this marker only records
-- schema version 17 for SQLite control planes, which keep working for every
-- unaffected Octop feature.
--
-- SQLite never stores tenant workflows, versions, bindings or revocations. The
-- fail-closed behaviour lives at the entry point: workbuddy_transaction raises
-- WORKBUDDY_POSTGRES_REQUIRED before a connection is checked out, and the
-- workflow repository calls require_postgres in its constructor. An aborting
-- marker here would break run_migrations at startup for every SQLite install.

UPDATE _schema_version SET version = 17;
