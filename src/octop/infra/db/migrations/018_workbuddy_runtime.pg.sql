-- Schema v18: WorkBuddy runtime execution facts (PostgreSQL only).
--
-- PostgreSQL is the execution fact store: every accepted execution, step/edge
-- result, approval decision, reconciliation, lease fence, quota movement,
-- audit record, notification and private chat message is durable here.
-- Delivery systems (Redis/queues) may only carry hints derived from these rows.
--
-- Invariants enforced below:
--   * UUID identifiers on every tenant-scoped row.
--   * compound (tenant_id, parent_id) foreign keys so a row can never point at
--     another tenant's parent.
--   * ENABLE + FORCE ROW LEVEL SECURITY on every table, with an inline
--     tenant predicate bound to the transaction-local app.tenant_id setting
--     (app.system = 'on' is the audited platform/migration path).
--   * Ledger tables (payloads, edge runs, audit, quota usage) have no UPDATE
--     and no DELETE policy: history is append-only and immutable.

CREATE TABLE workbuddy_executions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  workflow_id uuid NOT NULL,
  workflow_version_id uuid NOT NULL,
  workflow_version_hash text NOT NULL,
  definition_snapshot jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'running', 'waiting_approval', 'succeeded', 'failed', 'partial', 'cancelled')),
  trigger_type text NOT NULL CHECK (trigger_type IN ('manual', 'cron', 'webhook', 'event', 'api')),
  idempotency_scope text,
  idempotency_key text,
  idempotency_hash text,
  inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  outputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_code text,
  error_message text,
  fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
  created_by_user_id integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  cancel_requested_at timestamptz,
  UNIQUE (tenant_id, id),
  CHECK ((idempotency_scope IS NULL) = (idempotency_key IS NULL))
);

CREATE UNIQUE INDEX uq_wb_executions_idempotency ON workbuddy_executions (tenant_id, idempotency_scope, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_wb_executions_recent ON workbuddy_executions (tenant_id, created_at DESC);

CREATE INDEX idx_wb_executions_actor ON workbuddy_executions (tenant_id, created_by_user_id, created_at DESC);

CREATE INDEX idx_wb_executions_open ON workbuddy_executions (tenant_id, status)
  WHERE status IN ('pending', 'running', 'waiting_approval');

CREATE INDEX idx_wb_executions_workflow ON workbuddy_executions (tenant_id, workflow_id, created_at DESC);

CREATE TABLE workbuddy_execution_payloads (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('inputs', 'outputs', 'step_input', 'step_output', 'approval_params', 'approval_result', 'reconciliation_evidence')),
  node_id text,
  sha256 text NOT NULL,
  size_bytes integer NOT NULL CHECK (size_bytes >= 0),
  content jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_wb_payloads_execution ON workbuddy_execution_payloads (tenant_id, execution_id, created_at);

CREATE TABLE workbuddy_step_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  node_id text NOT NULL,
  node_type text NOT NULL CHECK (node_type IN ('tool', 'llm', 'condition', 'approval', 'transform')),
  attempt integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped', 'waiting_approval')),
  save_as text,
  input_sha256 text,
  output_sha256 text,
  output jsonb,
  error_code text,
  error_message text,
  fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  duration_ms integer,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, execution_id, node_id, attempt),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_wb_step_runs_execution ON workbuddy_step_runs (tenant_id, execution_id, started_at);

CREATE TABLE workbuddy_edge_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  edge_from text NOT NULL,
  edge_to text NOT NULL,
  branch text CHECK (branch IN ('true', 'false')),
  taken boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, execution_id, edge_from, edge_to),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE workbuddy_approval_requests (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  node_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')),
  required_approvals integer NOT NULL DEFAULT 1 CHECK (required_approvals >= 1),
  decided_approvals integer NOT NULL DEFAULT 0 CHECK (decided_approvals >= 0),
  params_sha256 text NOT NULL,
  params jsonb NOT NULL,
  token_hash text,
  token_expires_at timestamptz,
  token_consumed_at timestamptz,
  locked_workflow_version_id uuid NOT NULL,
  locked_workflow_version_hash text NOT NULL,
  decision text CHECK (decision IN ('approve', 'reject')),
  decided_by_user_id integer,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, execution_id, node_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE,
  CHECK ((status = 'pending') = (decided_at IS NULL))
);

CREATE INDEX idx_wb_approvals_pending ON workbuddy_approval_requests (tenant_id, created_at DESC)
  WHERE status = 'pending';

CREATE TABLE workbuddy_approval_candidates (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  approval_request_id uuid NOT NULL,
  user_id integer NOT NULL,
  department_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'abstained', 'invalidated')),
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, approval_request_id, user_id),
  FOREIGN KEY (tenant_id, approval_request_id) REFERENCES workbuddy_approval_requests (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_wb_approval_candidates_user ON workbuddy_approval_candidates (tenant_id, user_id, status, created_at DESC);

CREATE TABLE workbuddy_reconciliations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  node_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'matched', 'mismatched', 'unresolved')),
  external_ref text,
  evidence jsonb NOT NULL,
  evidence_sha256 text NOT NULL,
  recorded_by_user_id integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_wb_reconciliations_execution ON workbuddy_reconciliations (tenant_id, execution_id, created_at DESC);

CREATE TABLE workbuddy_jobs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('execution', 'improvement_proposal', 'knowledge_index', 'export', 'import', 'template_install', 'template_upgrade', 'trigger_delivery')),
  status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  execution_id uuid,
  requested_by_user_id integer,
  idempotency_key text,
  request_hash text,
  result jsonb,
  error_code text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, kind, idempotency_key)
);

CREATE INDEX idx_wb_jobs_recent ON workbuddy_jobs (tenant_id, created_at DESC);

CREATE INDEX idx_wb_jobs_requester ON workbuddy_jobs (tenant_id, requested_by_user_id, created_at DESC);

CREATE INDEX idx_wb_jobs_open ON workbuddy_jobs (tenant_id, status)
  WHERE status IN ('queued', 'running');

CREATE TABLE workbuddy_outbox (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  topic text NOT NULL,
  dedupe_key text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dispatched', 'failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  available_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, topic, dedupe_key)
);

CREATE INDEX idx_wb_outbox_pending ON workbuddy_outbox (tenant_id, available_at)
  WHERE status = 'pending';

CREATE TABLE workbuddy_leases (
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  lease_name text NOT NULL,
  holder text NOT NULL,
  fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
  acquired_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  released_at timestamptz,
  PRIMARY KEY (tenant_id, lease_name)
);

CREATE TABLE workbuddy_quota_reservations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  quota_key text NOT NULL,
  amount bigint NOT NULL CHECK (amount > 0),
  status text NOT NULL CHECK (status IN ('reserved', 'committed', 'released', 'expired')),
  scope text NOT NULL CHECK (scope IN ('execution', 'job', 'chat')),
  execution_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  settled_at timestamptz,
  UNIQUE (tenant_id, id)
);

CREATE INDEX idx_wb_quota_reservations_open ON workbuddy_quota_reservations (tenant_id, quota_key)
  WHERE status = 'reserved';

CREATE TABLE workbuddy_quota_usage (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  quota_key text NOT NULL,
  amount bigint NOT NULL CHECK (amount <> 0),
  direction text NOT NULL CHECK (direction IN ('consume', 'refund')),
  scope text NOT NULL CHECK (scope IN ('execution', 'job', 'chat')),
  execution_id uuid,
  job_id uuid,
  reservation_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  CHECK ((direction = 'consume') = (amount > 0))
);

CREATE INDEX idx_wb_quota_usage_window ON workbuddy_quota_usage (tenant_id, quota_key, recorded_at DESC);

CREATE TABLE workbuddy_audit_logs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  actor_user_id integer,
  actor_kind text NOT NULL CHECK (actor_kind IN ('member', 'admin', 'platform', 'system', 'trigger')),
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text,
  outcome text NOT NULL CHECK (outcome IN ('allowed', 'denied', 'failed')),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE INDEX idx_wb_audit_recent ON workbuddy_audit_logs (tenant_id, created_at DESC);

CREATE INDEX idx_wb_audit_resource ON workbuddy_audit_logs (tenant_id, resource_type, resource_id, created_at DESC);

CREATE TABLE workbuddy_notifications (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  user_id integer NOT NULL,
  kind text NOT NULL,
  title text NOT NULL,
  body text,
  resource_type text,
  resource_id text,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE INDEX idx_wb_notifications_user ON workbuddy_notifications (tenant_id, user_id, created_at DESC);

CREATE TABLE workbuddy_chat_sessions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  user_id integer NOT NULL,
  title text NOT NULL DEFAULT '',
  message_count integer NOT NULL DEFAULT 0 CHECK (message_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE INDEX idx_wb_chat_sessions_user ON workbuddy_chat_sessions (tenant_id, user_id, updated_at DESC);

CREATE TABLE workbuddy_chat_messages (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  session_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content text NOT NULL,
  model_revision text,
  usage jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, session_id) REFERENCES workbuddy_chat_sessions (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_wb_chat_messages_session ON workbuddy_chat_messages (tenant_id, session_id, created_at);

ALTER TABLE workbuddy_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_executions FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_executions_tenant ON workbuddy_executions FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_execution_payloads ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_execution_payloads FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_execution_payloads_tenant_read ON workbuddy_execution_payloads FOR SELECT
  USING (workbuddy_rls_visible(tenant_id));
CREATE POLICY workbuddy_execution_payloads_tenant_insert ON workbuddy_execution_payloads FOR INSERT
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_step_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_step_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_step_runs_tenant ON workbuddy_step_runs FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_edge_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_edge_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_edge_runs_tenant_read ON workbuddy_edge_runs FOR SELECT
  USING (workbuddy_rls_visible(tenant_id));
CREATE POLICY workbuddy_edge_runs_tenant_insert ON workbuddy_edge_runs FOR INSERT
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_approval_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_approval_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_approval_requests_tenant ON workbuddy_approval_requests FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_approval_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_approval_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_approval_candidates_tenant ON workbuddy_approval_candidates FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_reconciliations FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_reconciliations_tenant ON workbuddy_reconciliations FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_jobs_tenant ON workbuddy_jobs FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_outbox_tenant ON workbuddy_outbox FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_leases_tenant ON workbuddy_leases FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_quota_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_quota_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_quota_reservations_tenant ON workbuddy_quota_reservations FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_quota_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_quota_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_quota_usage_tenant_read ON workbuddy_quota_usage FOR SELECT
  USING (workbuddy_rls_visible(tenant_id));
CREATE POLICY workbuddy_quota_usage_tenant_insert ON workbuddy_quota_usage FOR INSERT
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_audit_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_audit_logs_tenant_read ON workbuddy_audit_logs FOR SELECT
  USING (workbuddy_rls_visible(tenant_id));
CREATE POLICY workbuddy_audit_logs_tenant_insert ON workbuddy_audit_logs FOR INSERT
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_notifications_tenant ON workbuddy_notifications FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_chat_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_chat_sessions_tenant ON workbuddy_chat_sessions FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_chat_messages FORCE ROW LEVEL SECURITY;
CREATE POLICY workbuddy_chat_messages_tenant_read ON workbuddy_chat_messages FOR SELECT
  USING (workbuddy_rls_visible(tenant_id));
CREATE POLICY workbuddy_chat_messages_tenant_insert ON workbuddy_chat_messages FOR INSERT
  WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_executions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_execution_payloads FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_step_runs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_edge_runs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_approval_requests FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_approval_candidates FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_reconciliations FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_jobs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_outbox FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_leases FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_quota_reservations FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_quota_usage FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_audit_logs FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_notifications FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_chat_sessions FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_chat_messages FROM PUBLIC;

UPDATE _schema_version SET version = 18;
