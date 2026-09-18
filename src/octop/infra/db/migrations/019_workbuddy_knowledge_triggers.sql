-- WorkBuddy knowledge bases and trigger registrations require PostgreSQL:
-- SQLite must fail closed, never serve tenant retrieval or webhook triggers as
-- an isolation fallback.
--
-- This marker records the schema version only. The tenant tables are missing on
-- purpose: every WorkBuddy knowledge/trigger entry point goes through
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises
-- WorkBuddyPostgresRequiredError (WORKBUDDY_POSTGRES_REQUIRED, mapped to a
-- controlled 503) before any SQL runs, so a SQLite control plane can keep
-- serving unaffected upstream features and never becomes an isolation fallback.

UPDATE _schema_version SET version = 19;
