# Architecture

## Current Runtime Architecture

The application is a modular Django monolith with an asynchronous transaction pipeline:

- `web`: Django REST API for authentication, accounts, transaction submission, and status polling
- `db`: PostgreSQL as the system of record
- `redis`: shared cache plus Celery broker/result backend
- `celery_worker`: background processing for transaction execution and recovery jobs
- `celery_beat`: periodic scheduling for outbox publication and stale-transaction recovery

The codebase is split into:

- `apps/accounts`: account persistence and account-facing API
- `apps/transactions`: transaction submission, idempotency, async processing, and status tracking
- `core`: framework settings, cache helpers, URL routing, and Celery bootstrap

PostgreSQL-specific storage notes:

- `transactions_transaction` is range-partitioned by `created_at` into monthly partitions plus a `DEFAULT` partition.
- Celery Beat runs scheduled partition maintenance to pre-create future monthly transaction partitions.
- Read traffic can be split between `default` (primary) and `replica` through selector-level routing for safe read-only queries.
- The Django app is PgBouncer-ready through env-driven connection tuning, including per-alias connection lifetime, health checks, and server-side cursor control.
- Global transaction idempotency is enforced via a separate `TransactionIdempotencyKey` registry table so monthly partitions do not need a cross-partition unique constraint.

## Transaction Flow

`POST /api/transactions/` is asynchronous and requires `Idempotency-Key`.
`POST /api/transactions/batches/` accepts up to 1000 items and processes them asynchronously.

Flow:

1. API validates request data and the idempotency header.
2. Service layer resolves source/target accounts and ownership.
3. The transaction is created in `pending` state with:
   - `initiated_by`
   - `idempotency_key`
   - `request_fingerprint`
4. A transaction outbox entry is stored in PostgreSQL in the same database transaction.
5. After commit, the application attempts to dispatch background processing from the outbox.
6. If broker delivery fails, the outbox row remains pending for later retry.
7. Worker moves the transaction through:
   - `pending`
   - `processing`
   - `completed` or `failed`
8. Clients poll `GET /api/transactions/{id}/status/`.
9. Clients can also subscribe to `ws://.../ws/transactions/{id}/` for realtime status updates.

### Batch Transaction Flow

`POST /api/transactions/batches/` accepts a list of item payloads plus a batch-level `Idempotency-Key`.

Flow:

1. API validates only the request shape, batch size, and duplicate item idempotency keys.
2. A `TransactionBatch` plus `TransactionBatchItem` rows are stored.
3. A Celery batch task asynchronously validates and processes each item.
4. Each valid item reuses the normal single-transaction async flow and outbox dispatch.
5. Clients poll `GET /api/transactions/batches/{id}/status/` for aggregate and per-item progress.
6. Clients can also subscribe to `ws://.../ws/transaction-batches/{id}/` for realtime batch status updates.

## Reliability Guarantees Implemented

- Idempotent transaction creation is enforced by:
  - application-level fingerprint checks
  - database unique constraint on the `TransactionIdempotencyKey` registry table
- Balance mutation is wrapped in a database transaction with deterministic lock ordering.
- Transaction acceptance and broker publication are decoupled with a PostgreSQL outbox row per accepted transaction.
- Celery delivery is hardened with:
  - late acknowledgements
  - retry with backoff and jitter
  - `reject_on_worker_lost`
  - worker prefetch multiplier `1`
- Pending outbox rows can be retried operationally with:
  - management command: `python manage.py publish_transaction_outbox`
- Pending outbox rows are also retried automatically by Celery Beat.
- Recovery path exists for stale `pending` and `processing` transactions:
  - Celery task: `recover_stuck_transfers_task`
  - management command: `python manage.py recover_stuck_transfers`
  - periodic scheduling via Celery Beat
- List caches use explicit TTL to avoid unbounded Redis growth.
- Production-style EC2 startup now uses a dedicated migration step before web and worker startup.

## Deployment Topology

### Local Docker

- `web` runs a dev entrypoint that applies migrations before `runserver`
- `celery_worker` and `celery_beat` run independently against the same codebase and database

### EC2 / Compose

- `migrate` is a one-shot release step
- `migrate` keeps using the direct primary PostgreSQL endpoint
- `pgbouncer` is a dedicated connection-management layer in front of PostgreSQL
- `web`, `celery_worker`, and `celery_beat` depend on successful migration completion
- `web`, `celery_worker`, and `celery_beat` use the pooled PostgreSQL endpoint exposed by `pgbouncer`
- `web` only handles collectstatic, optional admin bootstrap, and Gunicorn startup
- PgBouncer can sit in front of the primary and optional replica for app traffic, while the migrate/release step should keep using the direct primary endpoint

## Known Architectural Limits

These are the main remaining gaps before the system can be called banking-grade:

1. `Account.balance` is still mutable state rather than a projection from immutable ledger entries.
2. PostgreSQL concurrency semantics are relied on in production, but the current default test lane uses SQLite.
3. Observability is still basic text logging rather than structured transaction event logs and traces.
4. Replica routing is intentionally limited to safe selector-driven read paths; critical read-after-write flows still stay on the primary.

## Next Architecture Steps

Recommended priority order:

1. Design immutable ledger entries and move `Account.balance` to a derived projection.
2. Add pagination for list endpoints under sustained load.
3. Introduce fraud checks and policy-based transfer limits.
4. Add dashboards and alerts around outbox lag, queue depth, and failed transactions.
5. Introduce a release pipeline with explicit migrate, smoke test, and rollout phases.
