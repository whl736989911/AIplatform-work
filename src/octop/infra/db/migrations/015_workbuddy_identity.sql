-- Schema v15: WorkBuddy tenancy — SQLite fail-closed marker.
--
-- Every WorkBuddy table (tenants, departments, members, invitations, quotas,
-- governance audit) is PostgreSQL-only: SQLite has no row level security and no
-- transaction-local settings, so serving WorkBuddy on it could not keep tenants
-- apart. This file therefore creates nothing.
--
-- Failing closed happens at runtime, not in the migration: every WorkBuddy entry
-- point goes through octop.infra.db.workbuddy_context.workbuddy_transaction or
-- WorkBuddyIdentityRepo, which raise the controlled
-- WorkBuddyPostgresRequiredError (stable code WORKBUDDY_POSTGRES_REQUIRED) before
-- a single row is read or written, and the API reports it as a controlled 503.
-- A SQLite control plane therefore keeps upgrading and starting normally while
-- WorkBuddy stays unavailable.
--
-- Only the version watermark advances, so the migration does not repeat on the
-- next start and both dialects stay at comparable schema versions.

UPDATE _schema_version SET version = 15;
