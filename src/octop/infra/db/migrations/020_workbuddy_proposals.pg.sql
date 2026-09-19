-- Schema v20: WorkBuddy improvement proposals, reviews, shadow proofs and canary
-- evaluations (PostgreSQL only).
--
-- Scope: governed improvement of an immutable workflow version.
--   * a proposal fixes the base version id plus content hash and its own
--     immutable candidate version at creation: the candidate is compiled by
--     applying an RFC 6902 patch to the canonical base definition and then
--     re-diffing the final document, so patch path tricks cannot hide a change
--     from the boundary policy;
--   * trigger, approver, target and auth-boundary changes, approval bypass,
--     unapproved tools and private identifiers are refused before anything is
--     stored; low-risk/no-PII changes need one independent approval while
--     medium/high/PII need two plus a manual replay-only shadow proof;
--   * reviews, shadow runs and evaluations are append-only evidence rows, and
--     the creator can never review their own proposal (rejects win);
--   * promotion compare-and-swaps the workflow revision and either aborts,
--     starts shadow/canary traffic or applies the candidate as a new promoted
--     workflow version; a safety violation stops candidate traffic for good.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * statements are split on the regex "; \s* \n", so each top-level statement
--     must end its line with a semicolon, and a plpgsql body must never contain
--     a semicolon directly followed by a newline: end such a line with a
--     trailing comment ("; -- ...") so the split cannot land inside the body;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

-- ── Proposals ───────────────────────────────────────────────────────────────
-- One proposal fixes base_version_id/base_content_hash and its own immutable
-- candidate version.  The partial unique index keeps a single *pending*
-- proposal per workflow; shadow/canary proposals have already been promoted, so
-- a new idea may be drafted while they run and applying one supersedes the rest.
CREATE TABLE IF NOT EXISTS workbuddy_improvement_proposals (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  proposal_id              UUID NOT NULL DEFAULT gen_random_uuid(),
  workflow_id              UUID NOT NULL,
  workflow_revision        BIGINT NOT NULL,
  base_version_id          UUID NOT NULL,
  base_content_hash        TEXT NOT NULL,
  candidate_version_id     UUID NOT NULL,
  candidate_content_hash   TEXT NOT NULL,
  status                   TEXT NOT NULL,
  status_reason            TEXT,
  risk_level               TEXT NOT NULL,
  pii_involved             BOOLEAN NOT NULL DEFAULT FALSE,
  required_approvals       SMALLINT NOT NULL,
  requires_manual_shadow   BOOLEAN NOT NULL,
  change_summary           TEXT NOT NULL DEFAULT '',
  source_patch             JSONB NOT NULL,
  semantic_changes         JSONB NOT NULL,
  created_by_user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_by_membership_id UUID NOT NULL,
  canary_ratio_bp          INTEGER,
  canary_started_at        BIGINT,
  canary_stopped_at        BIGINT,
  canary_stop_reason       TEXT,
  applied_version_id       UUID,
  applied_at               BIGINT,
  created_at               BIGINT NOT NULL,
  updated_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, proposal_id),
  CONSTRAINT workbuddy_proposals_status_check CHECK (
    status IN ('pending','approved','rejected','shadowing','canary','applied','rolled_back','superseded','stale')
  ),
  CONSTRAINT workbuddy_proposals_risk_check CHECK (risk_level IN ('low','medium','high')),
  CONSTRAINT workbuddy_proposals_approvals_check CHECK (required_approvals BETWEEN 1 AND 2),
  CONSTRAINT workbuddy_proposals_pii_shadow_check CHECK (NOT pii_involved OR requires_manual_shadow),
  CONSTRAINT workbuddy_proposals_canary_check CHECK (canary_ratio_bp IS NULL OR canary_ratio_bp BETWEEN 1 AND 10000),
  CONSTRAINT workbuddy_proposals_canary_stop_check CHECK (canary_stopped_at IS NULL OR canary_started_at IS NOT NULL),
  CONSTRAINT workbuddy_proposals_applied_check CHECK ((status = 'applied') = (applied_version_id IS NOT NULL)),
  CONSTRAINT workbuddy_proposals_hash_check CHECK (
    base_content_hash ~ '^[0-9a-f]{64}$' AND candidate_content_hash ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT workbuddy_proposals_summary_check CHECK (char_length(change_summary) <= 500),
  CONSTRAINT workbuddy_proposals_workflow_fkey FOREIGN KEY (tenant_id, workflow_id)
    REFERENCES workbuddy_workflows(tenant_id, workflow_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_proposals_base_version_fkey FOREIGN KEY (tenant_id, workflow_id, base_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_id, workflow_version_id) ON DELETE RESTRICT,
  CONSTRAINT workbuddy_proposals_candidate_version_fkey FOREIGN KEY (tenant_id, workflow_id, candidate_version_id)
    REFERENCES workbuddy_workflow_versions(tenant_id, workflow_id, workflow_version_id) ON DELETE RESTRICT,
  CONSTRAINT workbuddy_proposals_creator_fkey FOREIGN KEY (tenant_id, created_by_membership_id)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workbuddy_proposals_pending_workflow
  ON workbuddy_improvement_proposals(tenant_id, workflow_id)
  WHERE status IN ('pending','approved');
CREATE INDEX IF NOT EXISTS idx_workbuddy_proposals_workflow
  ON workbuddy_improvement_proposals(tenant_id, workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbuddy_proposals_status
  ON workbuddy_improvement_proposals(tenant_id, status, updated_at DESC);

-- ── Reviews (immutable) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workbuddy_proposal_reviews (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  review_id                UUID NOT NULL DEFAULT gen_random_uuid(),
  proposal_id              UUID NOT NULL,
  reviewer_user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  reviewer_membership_id   UUID NOT NULL,
  decision                 TEXT NOT NULL,
  comment                  TEXT NOT NULL DEFAULT '',
  created_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, review_id),
  CONSTRAINT workbuddy_proposal_reviews_decision_check CHECK (decision IN ('approved','rejected')),
  CONSTRAINT workbuddy_proposal_reviews_comment_check CHECK (char_length(comment) <= 2000),
  CONSTRAINT workbuddy_proposal_reviews_unique_reviewer UNIQUE (tenant_id, proposal_id, reviewer_user_id),
  CONSTRAINT workbuddy_proposal_reviews_proposal_fkey FOREIGN KEY (tenant_id, proposal_id)
    REFERENCES workbuddy_improvement_proposals(tenant_id, proposal_id) ON DELETE CASCADE,
  CONSTRAINT workbuddy_proposal_reviews_reviewer_fkey FOREIGN KEY (tenant_id, reviewer_membership_id)
    REFERENCES workbuddy_tenant_members(tenant_id, membership_id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_proposal_reviews_proposal
  ON workbuddy_proposal_reviews(tenant_id, proposal_id, created_at);

-- ── Shadow runs (append-only replay evidence) ───────────────────────────────
CREATE TABLE IF NOT EXISTS workbuddy_proposal_shadow_runs (
  tenant_id             UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  shadow_run_id         UUID NOT NULL DEFAULT gen_random_uuid(),
  proposal_id           UUID NOT NULL,
  candidate_version_id  UUID NOT NULL,
  source_execution_id   UUID,
  replay_only           BOOLEAN NOT NULL,
  live_side_effects     INTEGER NOT NULL DEFAULT 0,
  settled               BOOLEAN NOT NULL DEFAULT FALSE,
  evidence_sha256       TEXT NOT NULL,
  settled_at            BIGINT,
  created_at            BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, shadow_run_id),
  CONSTRAINT workbuddy_proposal_shadow_live_check CHECK (live_side_effects >= 0),
  CONSTRAINT workbuddy_proposal_shadow_settled_check CHECK (NOT settled OR settled_at IS NOT NULL),
  CONSTRAINT workbuddy_proposal_shadow_evidence_check CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT workbuddy_proposal_shadow_proposal_fkey FOREIGN KEY (tenant_id, proposal_id)
    REFERENCES workbuddy_improvement_proposals(tenant_id, proposal_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_proposal_shadow_proposal
  ON workbuddy_proposal_shadow_runs(tenant_id, proposal_id, created_at);

-- ── Canary evaluations (append-only gate evidence) ──────────────────────────
CREATE TABLE IF NOT EXISTS workbuddy_proposal_evaluations (
  tenant_id                UUID NOT NULL REFERENCES workbuddy_tenants(tenant_id) ON DELETE CASCADE,
  evaluation_id            UUID NOT NULL DEFAULT gen_random_uuid(),
  proposal_id              UUID NOT NULL,
  phase                    TEXT NOT NULL,
  window_start             BIGINT NOT NULL,
  window_end               BIGINT NOT NULL,
  baseline_settled_runs    INTEGER NOT NULL,
  candidate_settled_runs   INTEGER NOT NULL,
  baseline_success_rate    DOUBLE PRECISION NOT NULL,
  candidate_success_rate   DOUBLE PRECISION NOT NULL,
  baseline_p95_latency_ms  DOUBLE PRECISION NOT NULL,
  candidate_p95_latency_ms DOUBLE PRECISION NOT NULL,
  baseline_avg_tokens      DOUBLE PRECISION NOT NULL,
  candidate_avg_tokens     DOUBLE PRECISION NOT NULL,
  -- Approval and reconciliation waits, reported separately from active time.
  baseline_wait_ms         DOUBLE PRECISION NOT NULL DEFAULT 0,
  candidate_wait_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
  safety_violations        INTEGER NOT NULL DEFAULT 0,
  passed                   BOOLEAN NOT NULL,
  safety_stop              BOOLEAN NOT NULL,
  full_days                INTEGER NOT NULL,
  failures                 JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at               BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, evaluation_id),
  CONSTRAINT workbuddy_proposal_evaluations_phase_check CHECK (phase IN ('shadow','canary')),
  CONSTRAINT workbuddy_proposal_evaluations_window_check CHECK (window_end >= window_start),
  CONSTRAINT workbuddy_proposal_evaluations_counts_check CHECK (
    baseline_settled_runs >= 0 AND candidate_settled_runs >= 0 AND safety_violations >= 0
    AND full_days >= 0 AND baseline_wait_ms >= 0 AND candidate_wait_ms >= 0
  ),
  CONSTRAINT workbuddy_proposal_evaluations_rates_check CHECK (
    baseline_success_rate BETWEEN 0 AND 1 AND candidate_success_rate BETWEEN 0 AND 1
  ),
  CONSTRAINT workbuddy_proposal_evaluations_latency_check CHECK (
    baseline_p95_latency_ms >= 0 AND candidate_p95_latency_ms >= 0
    AND baseline_avg_tokens >= 0 AND candidate_avg_tokens >= 0
  ),
  CONSTRAINT workbuddy_proposal_evaluations_failures_check CHECK (jsonb_typeof(failures) = 'array'),
  CONSTRAINT workbuddy_proposal_evaluations_proposal_fkey FOREIGN KEY (tenant_id, proposal_id)
    REFERENCES workbuddy_improvement_proposals(tenant_id, proposal_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workbuddy_proposal_evaluations_proposal
  ON workbuddy_proposal_evaluations(tenant_id, proposal_id, created_at);

-- ── Guards ──────────────────────────────────────────────────────────────────
-- Base and candidate are frozen at creation, and terminal statuses stay terminal.
CREATE OR REPLACE FUNCTION workbuddy_proposals_guard_identity() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $wb$ -- proposal identity is frozen
BEGIN
  IF NEW.proposal_id <> OLD.proposal_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.workflow_id <> OLD.workflow_id OR NEW.workflow_revision <> OLD.workflow_revision OR NEW.base_version_id <> OLD.base_version_id OR NEW.base_content_hash <> OLD.base_content_hash OR NEW.candidate_version_id <> OLD.candidate_version_id OR NEW.candidate_content_hash <> OLD.candidate_content_hash OR NEW.risk_level <> OLD.risk_level OR NEW.required_approvals <> OLD.required_approvals OR NEW.requires_manual_shadow <> OLD.requires_manual_shadow THEN RAISE EXCEPTION 'workbuddy: proposal base and candidate are immutable' USING ERRCODE = '23514'; END IF; -- fixed base and candidate
  IF OLD.status IN ('applied','rejected','aborted','superseded','stale') AND NEW.status <> OLD.status THEN RAISE EXCEPTION 'workbuddy: proposal status is terminal' USING ERRCODE = '23514'; END IF; -- terminal states never move
  IF OLD.status = 'applied' AND NEW.applied_version_id IS DISTINCT FROM OLD.applied_version_id THEN RAISE EXCEPTION 'workbuddy: applied version is immutable' USING ERRCODE = '23514'; END IF; -- applied pointer frozen
  RETURN NEW; -- accepted
END $wb$;

DROP TRIGGER IF EXISTS workbuddy_proposals_immutable_id ON workbuddy_improvement_proposals;
CREATE TRIGGER workbuddy_proposals_immutable_id BEFORE UPDATE ON workbuddy_improvement_proposals
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_immutable_column('proposal_id');

DROP TRIGGER IF EXISTS workbuddy_proposals_identity_guard ON workbuddy_improvement_proposals;
CREATE TRIGGER workbuddy_proposals_identity_guard BEFORE UPDATE ON workbuddy_improvement_proposals
  FOR EACH ROW EXECUTE FUNCTION workbuddy_proposals_guard_identity();

DROP TRIGGER IF EXISTS workbuddy_proposal_reviews_append_only ON workbuddy_proposal_reviews;
CREATE TRIGGER workbuddy_proposal_reviews_append_only BEFORE UPDATE OR DELETE ON workbuddy_proposal_reviews
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_proposal_reviews_no_truncate ON workbuddy_proposal_reviews;
CREATE TRIGGER workbuddy_proposal_reviews_no_truncate BEFORE TRUNCATE ON workbuddy_proposal_reviews
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_proposal_shadow_append_only ON workbuddy_proposal_shadow_runs;
CREATE TRIGGER workbuddy_proposal_shadow_append_only BEFORE UPDATE OR DELETE ON workbuddy_proposal_shadow_runs
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_proposal_shadow_no_truncate ON workbuddy_proposal_shadow_runs;
CREATE TRIGGER workbuddy_proposal_shadow_no_truncate BEFORE TRUNCATE ON workbuddy_proposal_shadow_runs
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_proposal_evaluations_append_only ON workbuddy_proposal_evaluations;
CREATE TRIGGER workbuddy_proposal_evaluations_append_only BEFORE UPDATE OR DELETE ON workbuddy_proposal_evaluations
  FOR EACH ROW EXECUTE FUNCTION workbuddy_guard_append_only();

DROP TRIGGER IF EXISTS workbuddy_proposal_evaluations_no_truncate ON workbuddy_proposal_evaluations;
CREATE TRIGGER workbuddy_proposal_evaluations_no_truncate BEFORE TRUNCATE ON workbuddy_proposal_evaluations
  FOR EACH STATEMENT EXECUTE FUNCTION workbuddy_guard_append_only();

-- ── Row level security: every tenant table is isolated by app.tenant_id ──────
ALTER TABLE workbuddy_improvement_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_improvement_proposals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_improvement_proposals;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_improvement_proposals
  FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_proposal_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_proposal_reviews FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_proposal_reviews;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_proposal_reviews
  FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_proposal_shadow_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_proposal_shadow_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_proposal_shadow_runs;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_proposal_shadow_runs
  FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_proposal_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_proposal_evaluations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_tenant_isolation ON workbuddy_proposal_evaluations;
CREATE POLICY workbuddy_tenant_isolation ON workbuddy_proposal_evaluations
  FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privileges ──────────────────────────────────────────────────────────────

REVOKE ALL ON TABLE workbuddy_improvement_proposals FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_proposal_reviews FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_proposal_shadow_runs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_proposal_evaluations FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION workbuddy_proposals_guard_identity() FROM PUBLIC;

UPDATE _schema_version SET version = 20;
