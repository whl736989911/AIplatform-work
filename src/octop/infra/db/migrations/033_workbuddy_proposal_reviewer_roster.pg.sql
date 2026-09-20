-- Schema v33: the reviewer roster of an improvement proposal lives on the
-- proposal row, not in the governance trail (PostgreSQL only).
--
-- Why this is a schema change and not a query tweak: the roster was reconstructed
-- by reading the *latest* assignment event out of workbuddy_tenant_audit_events
-- (`ORDER BY created_at DESC, event_id DESC`).  That table stores unix seconds and
-- a random UUID, so two assignments inside the same second are ordered by a coin
-- flip — a reassignment that happens immediately after the first one can report
-- the roster it just replaced.  CI caught exactly that: the roster came back with
-- the previous reviewer still on it.
--
-- The audit event stays (history is append-only governance evidence); the row
-- column is the current state.  A row is the natural home for "in force now":
-- there is nothing to order, so nothing to guess.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * each statement ends with its semicolon and no statement contains a
--     semicolon directly followed by a newline;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file;
--   * every statement is idempotent so a half-applied run can simply be repeated.

ALTER TABLE workbuddy_improvement_proposals
  ADD COLUMN IF NOT EXISTS reviewers JSONB;

-- An existing proposal has no roster yet; the assignment route fills it on the
-- first call.  ``NULL`` therefore means "nobody assigned yet" and is rendered as
-- an empty roster by the repository, never as a fabricated member.
COMMENT ON COLUMN workbuddy_improvement_proposals.reviewers IS
  'Current independent reviewer roster: JSON array of {membership_id, user_id}; NULL until a manager staffs the review.';
