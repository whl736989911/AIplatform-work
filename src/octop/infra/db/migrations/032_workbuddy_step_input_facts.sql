-- Schema v32: step input and model usage facts — SQLite fail-closed watermark.
--
-- The rows that carry these facts live in PostgreSQL only, exactly like the rest
-- of the WorkBuddy runtime (executions, step runs, payloads, leases): this file
-- creates nothing on SQLite on purpose. Every WorkBuddy entry point runs through
-- octop.infra.db.workbuddy_context.workbuddy_transaction, which raises the
-- controlled WorkBuddyPostgresRequiredError before a single row is read or
-- written, so a SQLite control plane refuses to answer a runtime request instead
-- of reporting a step as having no input and no usage when it simply has no fact
-- store. The PostgreSQL side of this version is
-- 032_workbuddy_step_input_facts.pg.sql.
--
-- Only the version watermark advances so the upgrade does not repeat.

UPDATE _schema_version SET version = 32;
