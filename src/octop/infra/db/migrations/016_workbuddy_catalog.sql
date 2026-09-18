-- Schema v16: WorkBuddy connector-credential governance is PostgreSQL-only.
--
-- SQLite keeps the Octop control plane working but must fail closed for
-- WorkBuddy: octop.infra.db.workbuddy_context raises
-- WorkBuddyPostgresRequiredError before any statement runs, and the
-- /api/v1/connector-credentials and /api/v1/tenant-capabilities routes report
-- that as a controlled 503. There is therefore no SQLite schema to create here;
-- only the version watermark advances so both dialects stay comparable and
-- future migrations are not skipped on SQLite installs.

UPDATE _schema_version SET version = 16;
