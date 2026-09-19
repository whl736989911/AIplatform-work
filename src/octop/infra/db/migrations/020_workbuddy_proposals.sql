-- Schema v20: WorkBuddy improvement proposals — SQLite fail-closed marker.
--
-- Improvement proposals, reviews, shadow proofs and canary evaluations live in
-- PostgreSQL only: they compare-and-swap workflow revisions, depend on FORCE
-- RLS for tenant isolation, and their evidence rows are append-only. This file
-- therefore creates nothing on SQLite.
--
-- Failing closed happens at runtime, not in the migration: every proposal entry
-- point goes through octop.infra.db.workbuddy_context.workbuddy_transaction (or
-- WorkBuddyProposalsRepo, which requires PostgreSQL in its constructor), which
-- raises the controlled WorkBuddyPostgresRequiredError (stable code
-- WORKBUDDY_POSTGRES_REQUIRED) before a single row is read or written, and the
-- API reports it as a controlled 503. A SQLite control plane therefore keeps
-- upgrading and starting normally while WorkBuddy proposals stay unavailable.
--
-- Only the version watermark advances, so the migration does not repeat on the
-- next start and both dialects stay at comparable schema versions.

UPDATE _schema_version SET version = 20;
