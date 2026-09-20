-- Schema v31: WorkBuddy re-authentication credentials — SQLite fail-closed marker.
--
-- The re-authentication credential table is PostgreSQL only, exactly like the
-- rest of the WorkBuddy lifecycle slice (export jobs, redeem tokens, deletion
-- requests, ledger, tombstones). This file creates nothing on SQLite on
-- purpose: /auth/reauthenticate and /exports/{id}/download-challenge both run
-- through octop.infra.db.workbuddy_context.workbuddy_transaction, which raises
-- the controlled WorkBuddyPostgresRequiredError before a single row is read or
-- written, so a SQLite control plane refuses to mint or consume a credential
-- instead of pretending to remember one.
--
-- Only the version watermark advances so the upgrade does not repeat.

UPDATE _schema_version SET version = 31;
