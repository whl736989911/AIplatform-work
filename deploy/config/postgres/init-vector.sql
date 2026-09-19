-- Instance-level init only (docker-entrypoint-initdb.d on first volume).
-- NOT part of Octop control-plane migrations (see ADR 002).
--
-- This is the deployment's own copy: a production install ships `deploy/` as the
-- unit, so the compose file must not reach into the repository's development
-- tree (`docker/postgres/init-vector.sql`, used by
-- docker/docker-compose.postgres.yml) for a file the stack needs at boot.
CREATE EXTENSION IF NOT EXISTS vector;
