# WorkBuddy — a multi-tenant enterprise AI workflow platform built on Octop

> **Read this first.** This repository is **not** a release branch of upstream [Octop](https://github.com/TencentCloud/Octop), and this is
> **not** its product documentation. It uses Octop (MIT, see `LICENSE`) as a **substrate**, but its product direction is a
> **multi-tenant enterprise workflow platform**: employees build workflows, run them, and keep improving them — with human steps
> (approval, asking a question, reviewing an output) inside the workflow rather than bolted onto it.
> Upstream's README describes a different product (a self-hosted personal/team AI assistant, channels, FnOS packaging, remote phone,
> desktop installers, its plugin and expert marketplace). **Do not judge this repository by it.** If you are looking for upstream,
> go to its repository; if you want to know what this one delivers, read "What is delivered" below and `docs/plan/`.

## What this repository is

- A **multi-tenant platform**: tenants, departments, members and roles; four-level object permissions (company / department / personal / explicit
  grant); cross-tenant isolation enforced by PostgreSQL row-level security (RLS).
- A **workflow platform**: definition → compile (reporting several diagnostics at once) → immutable versions → activate / roll back →
  execution worker → per-node triage and version diff.
- A **human-in-the-loop platform**: approvals (authorising an external write or a general review), **asking a person a question mid-run**
  (the run parks where it is instead of being cancelled and restarted), and **output review** (after the fact and append-only: accept,
  correct, or re-run).
- A platform that **improves itself**: correction capture → attribution → analyser proposals → shadow / canary evaluation → promotion;
  plus a template marketplace.
- An **operable service**: migrations (PostgreSQL and SQLite control planes), jobs and audit, an outbox with a dispatcher, rate limiting,
  quotas, leases and fencing tokens.

## What this repository is not

- Not a mirror or release channel of upstream Octop; upstream's README, badges and screenshots belong to upstream.
- Not defined as a "personal assistant / chatbot" — the chat and agent substrate is still in the tree (see below), but it is not what this
  repository delivers.
- It makes no promise about upstream's personal- and NAS-oriented packaging or installation forms.
- It is **not** production-ready for third parties today: connector / model-gateway adapters and trigger delivery are **deployment wiring**,
  the outbox needs a publisher port, and tenant-level metrics plus production deregistration compliance are **product decisions**
  (see "Current boundaries").

## What is delivered

### WorkBuddy (this repository's direction)

| Capability | What it means |
|---|---|
| Tenancy and identity | Tenants, departments, members, roles; member and tenant status are read from the fact store, not from the token |
| Workflows | Authoritative schema (`contracts/workflow-v1.schema.json`), a compiler that aggregates diagnostics, immutable versions with ETags, explicit activate / rollback / archive |
| Execution | Acceptance only queues; a worker claims atomically (locks the tenant row, takes a running slot, holds a lease with a monotonic fence) and the next worker takes over after a crash |
| Human in the loop | Approvals (one-time challenge token, candidate approvers), **questions** (`waiting_input`, the run continues after the answer), **output reviews** (after the fact, append-only) |
| Governance | Tool / model / knowledge-base / approver reachability resolved **per caller**; capability grants to departments and members; four-level object permissions |
| Knowledge base | Documents indexed as real jobs; a text-only migration channel (personal installs keep chunk text and vectors, not originals) |
| Improvement loop | Correction capture → attribution → proposals → shadow replay → canary evaluation → promotion, with independently assigned reviewers |
| Platform services | Jobs, notifications, audit, outbox + dispatcher, rate limiting, quotas (monthly execution reservation and concurrency slot), platform tool registry |

### The Octop substrate (still in the tree, but not this repository's direction)

Chat and agents, skills / subagents, the knowledge-base UI, the connector gateway, channels (Feishu / DingTalk / WeCom and others), the
dashboard, the CLI, desktop shells and packaging scripts. They build and parts of them are reused by WorkBuddy (the console, auth, the
database layer), but **do not** read upstream's productised descriptions of these as promises of this repository.

## Getting started

### Requirements

- Python **≥ 3.12** with [`uv`](https://docs.astral.sh/uv/) (every Makefile target runs through `uv run`).
- **PostgreSQL is required** (WorkBuddy tenant workflows need it) together with the `vector` extension, installed as a prerequisite.
- **Redis** backs rate limiting. In production, run the worker tier as well, or let a single-process install host it.

### Install and initialise

```bash
uv sync                       # Python dependencies
uv run octop init             # initialise the local control plane (runs migrations)

cd dashboard && npm install   # console dependencies
```

### Run

```bash
uv run octop run              # API + web console (foreground)
make dev                      # backend and console dev servers together
cd dashboard && npm run dev    # console only (vite --host)
```

### Docker

```bash
docker compose -f deploy/compose.production.yml up -d --build
```

The production compose file runs the execution worker as its own tier. A single-process install hosts it inside `octop run`;
`OCTOP_WORKBUDDY_WORKER=off` disables that.

## Command line

```bash
uv run octop init                       # control plane + migrations
uv run octop run                        # API and console
uv run octop workbuddy worker            # execution worker tier (needs PostgreSQL; migrates on start)
uv run octop workbuddy cel -e '1 + 1'    # evaluate one expression in the bounded CEL sandbox (contract probe, no server)
```

## API at a glance

Every business endpoint lives under `/api/v1`. The **authoritative list** is `contracts/route-manifest.json`, which carries each route's
origin, success status and authorization requirement (`counts.source` is the upstream contract document's route count; the rest were added
during implementation). The frequent entry points:

| Area | Entry points |
|---|---|
| Workflows | `POST /workflows`, `POST /workflows/{id}/activate`, `POST /workflows/{id}/execute`, `GET /workflow-definitions/metadata` |
| Executions | `GET /executions`, `GET /executions/{id}` (per-node triage and wait reasons), `POST /executions/{id}/cancel` |
| Approvals | `GET /approval-requests`, `POST /approval-requests/{id}/challenge`, `POST /executions/{id}/resume` |
| Questions (human in the loop) | `GET /executions/{id}/input-requests`, `POST /executions/{id}/input-requests/{rid}/answer`, `GET /input-requests` |
| Output reviews | `POST /executions/{id}/output-review`, `GET /executions/{id}/output-review`, `POST /executions/{id}/output-review/decisions`, `GET /output-reviews` |
| Governance and operations | `GET /tenant-capabilities`, `GET /jobs`, `GET /notifications`, `GET /audit-logs`, `GET /usage` |

## Verifying

```bash
make lint          # ruff check + ruff format --check (src and tests)
make typecheck     # mypy src/octop
make test          # pytest -n N -m "not live" (everything that needs no external credentials)

# Tests that need real PostgreSQL / Redis (the same set as CI's Live database tests job)
OCTOP_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/octop_test \
OCTOP_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
uv run pytest -m "postgresql or redis"

cd dashboard && npx tsc -b && npx vitest run && npx eslint .
```

CI runs `Python 3.12`, `Windows / Python 3.12`, `Live tests (real credentials)`, `Dashboard` and `CodeQL`. The live database job is the
**only** one that covers migrations, RLS and real database behaviour — unit tests alone are not enough to call a change usable.

## Deployment notes

- **Migrations**: `octop init` (and `octop workbuddy worker`, backup and admin commands) apply them. The version watermark stays monotonic on
  both control planes; WorkBuddy's tables and execution facts exist in PostgreSQL only, so SQLite advances the watermark and fails closed.
- **RLS**: every WorkBuddy business table is `ENABLE` + `FORCE ROW LEVEL SECURITY` with a policy on the transaction-local `app.tenant_id`.
  Platform-wide operations use an explicit platform context instead of relaxing a policy.
- **Roles**: this repository creates no runtime roles and grants nothing implicitly. Migrations only `REVOKE ALL ... FROM PUBLIC`;
  creating the runtime role is the deployment's job.
- **Worker**: execution, outbox dispatch and deadline settlement all need a worker running. Each is idempotent and re-entrant.

## Current boundaries

1. Connector / model-gateway adapters and trigger delivery are **deployment wiring**: without them the affected step fails closed with a
   dependency-unavailable error instead of quietly producing an empty result.
2. The outbox only writes: inject a publisher port and the dispatcher starts delivering. Without one it refuses to run rather than mark an
   event nobody received as delivered.
3. Tenant-level metric definitions and production deregistration / compliance sign-off are **product decisions**; do not promise them yet.
4. Batch progress, contract divergences and integration verdicts live in `docs/plan/` (`two-machine-workstreams.md` is the plan, `ledger/`
   holds the two machines' and the integrator's state).

## Repository layout

```
src/octop/
  api/routers/            HTTP layer (workbuddy_*.py are WorkBuddy's endpoints)
  infra/workbuddy/        workflow compiler / runtime / approvals / knowledge / proposals / marketplace / worker
  infra/db/               data access and migrations (migrations/ carries both PostgreSQL and SQLite sides)
  infra/rbac/             object-permission skeleton (four scopes plus explicit grants)
  cli/                    command line (including octop workbuddy …)
contracts/                authoritative contracts: workflow-v1.schema.json, route-manifest.json
dashboard/                console (React + Vite + antd)
docs/                     architecture, API, CLI, configuration and ADRs
tests/                    unit / integration (PG- and Redis-marked cases need real dependencies)
docs/plan/                delivery plan, ledgers and integration verdicts
```

## Licence and upstream

- This repository is **MIT** (`LICENSE`, copyright Octop). Changes made here are offered under the same licence.
- Upstream: [TencentCloud/Octop](https://github.com/TencentCloud/Octop). This repository does **not** share its product direction; the parts
  it reuses remain bound by upstream's licence and copyright.
