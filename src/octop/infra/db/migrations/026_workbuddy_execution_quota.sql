-- Schema v26 marker for SQLite control planes.
--
-- The quota metric catalogue lives in PostgreSQL only; SQLite never stores a
-- quota table. Recording the version keeps the chain monotonic for SQLite.

UPDATE _schema_version SET version = 26;
