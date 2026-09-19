-- Schema v27: the execution worker's admission queue (PostgreSQL only).
--
-- The published deployment topology separates the API tier ("no in-process
-- persistent business state") from a worker tier that reads its work from this
-- database, and names the columns a claim stores:
--
--   owner_worker_id  -> workbuddy_leases.holder
--   lease_expires_at -> workbuddy_leases.expires_at
--   fencing_token    -> workbuddy_leases.fence   (monotonic; bumped on takeover)
--
-- No new table is needed: a claim is one short transaction that locks the
-- tenant's row, counts the tenant's live concurrency reservations, reserves one
-- for the execution when it does not already hold it, takes the lease and moves
-- the execution to ``running``. What the schema lacked is an index that makes
-- finding the oldest waiting execution cheap for a worker that scans across
-- tenants, and a status that means "accepted, not admitted yet" -- which 023
-- already introduced as ``queued``.
--
-- ``running`` rows are claimable too: a worker that died mid-run leaves a lease
-- that expires, and the next claimant takes over from there with the fence it
-- already carries.

CREATE INDEX IF NOT EXISTS idx_wb_executions_claimable
  ON workbuddy_executions (created_at, id)
  WHERE status IN ('queued', 'running');

-- A worker that takes over a dead worker's execution must not need a second
-- slot for it, so the slot is found by execution rather than by reservation id.
CREATE INDEX IF NOT EXISTS idx_wb_quota_reservations_execution
  ON workbuddy_quota_reservations (tenant_id, execution_id)
  WHERE status = 'reserved' AND execution_id IS NOT NULL;

UPDATE _schema_version SET version = 27;
