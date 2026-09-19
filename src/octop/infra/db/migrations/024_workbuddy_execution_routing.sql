-- Schema v24 marker for SQLite control planes.
--
-- Execution routing lives in PostgreSQL only; SQLite never stores executions.
-- Recording the version keeps the migration chain monotonic for SQLite installs.

UPDATE _schema_version SET version = 24;
