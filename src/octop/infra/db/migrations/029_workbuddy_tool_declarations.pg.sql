-- Schema v29: the platform tool registry records what a tool *is*, not only
-- where it comes from (PostgreSQL only).
--
-- Contract §4.6.1 defines the platform tool table as
--
--   id, tool_key, revision, input_schema jsonb, output_schema jsonb,
--   effect_class, supports_idempotency, supports_result_lookup, sandbox_verified, ...
--
-- and contract line 891 states the rule that consumes two of those fields:
-- "只有工具已确认幂等键语义才允许自动重复外部写" -- an external write may only be
-- re-sent automatically when the tool's idempotency semantics are confirmed.
-- Without them the engine could neither validate a tool's result against its
-- registered schema nor tell a read-only tool from a write that must not be
-- repeated.
--
-- Every default is the conservative one: an unclassified tool is treated as an
-- external write, is not idempotent, and is not sandbox verified, so nothing
-- becomes safer by omission.

ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS input_schema jsonb;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS output_schema jsonb;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS effect_class text NOT NULL DEFAULT 'external_write';
ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS supports_idempotency boolean NOT NULL DEFAULT false;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS supports_result_lookup boolean NOT NULL DEFAULT false;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD COLUMN IF NOT EXISTS sandbox_verified boolean NOT NULL DEFAULT false;

ALTER TABLE workbuddy_platform_tool_revisions
  DROP CONSTRAINT IF EXISTS workbuddy_platform_tool_revisions_effect_class_check;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD CONSTRAINT workbuddy_platform_tool_revisions_effect_class_check
    CHECK (effect_class IN ('read_only', 'external_write'));

-- A tool that claims to support result lookup must also declare where its
-- result comes from: nothing can fetch a result without a schema to check it.
ALTER TABLE workbuddy_platform_tool_revisions
  DROP CONSTRAINT IF EXISTS workbuddy_platform_tool_revisions_result_lookup_check;
ALTER TABLE workbuddy_platform_tool_revisions
  ADD CONSTRAINT workbuddy_platform_tool_revisions_result_lookup_check
    CHECK (NOT supports_result_lookup OR output_schema IS NOT NULL);

UPDATE _schema_version SET version = 29;
