-- WorkBuddy runtime requires PostgreSQL: SQLite must fail closed, never run
-- tenant workflows as an isolation fallback.
--
-- SQLite has no row-level security, no FORCE RLS and no transaction-local
-- app.tenant_id, so the runtime fact model cannot exist here. This marker only
-- advances the schema watermark; the fail-closed guarantee is enforced at run
-- time by workbuddy_transaction / WorkBuddyRuntimeService, which raise
-- WORKBUDDY_POSTGRES_REQUIRED before touching any table.

UPDATE _schema_version SET version = 18;
