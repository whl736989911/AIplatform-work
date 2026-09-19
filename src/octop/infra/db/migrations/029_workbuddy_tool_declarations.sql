-- Schema v29 marker for SQLite control planes.
--
-- The WorkBuddy platform catalogue lives in PostgreSQL only; SQLite never stores
-- these tables. Recording the version keeps the chain monotonic for SQLite.

UPDATE _schema_version SET version = 29;
