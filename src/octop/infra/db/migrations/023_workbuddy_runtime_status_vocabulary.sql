-- Schema v23 marker for SQLite control planes.
--
-- The runtime status vocabulary lives in PostgreSQL only; SQLite never stores
-- executions. Recording the version keeps the migration chain monotonic for
-- SQLite installs, which keep working for every unaffected Octop feature.

UPDATE _schema_version SET version = 23;
