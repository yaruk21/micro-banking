# Architecture

## Current Runtime Architecture

The application is a modular Django monolith with an asynchronous transaction pipeline:

- `web`: Django REST API for authentication, accounts, transaction submission, and status polling
- `db`: PostgreSQL as the system of record
- `redis`: shared cache plus Celery broker/result backend
- `celery_worker`: background processing for transaction execution and recovery jobs

The codebase is split into:

- `apps/accounts`: account persistence and account-facing API
- `apps/transactions`: transaction submission, idempotency, async processing, and status tracking
- `core`: framework settings, cache helpers, URL routing, and Celery bootstrap

## Transaction Flow

`POST /api/transactions/` is asynchronous and requires `Idempotency-Key`.

Flow:

1. API validates request data and the idempotency header.
2. Service layer resolves source/target accounts and ownership.
3. The transaction is created in `pending` state with:
   - `initiated_by`
   - `idempotency_key`
   - `request_fingerprint`
4. Celery enqueues background processing.
5. Worker moves the transaction through:
   - `pending`
   - `processing`
   - `completed` or `failed`
6. Clients poll `GET /api/transactions/{id}/status/`.

## Reliability Guarantees Implemented

- Idempotent transaction creation is enforced by:
  - application-level fingerprint checks
  - database unique constraint on `(initiated_by, idempotency_key)`
- Balance mutation is wrapped in a database transaction with deterministic lock ordering.
- Celery delivery is hardened with:
  - late acknowledgements
  - retry with backoff and jitter
  - `reject_on_worker_lost`
  - worker prefetch multiplier `1`
- Recovery path exists for stale `pending` and `processing` transactions:
  - Celery task: `recover_stuck_transfers_task`
  - management command: `python manage.py recover_stuck_transfers`
- List caches use explicit TTL to avoid unbounded Redis growth.
- Production-style EC2 startup now uses a dedicated migration step before web and worker startup.

## Deployment Topology

### Local Docker

- `web` runs a dev entrypoint that applies migrations before `runserver`
- `celery_worker` runs independently against the same codebase and database

### EC2 / Compose

- `migrate` is a one-shot release step
- `web` and `celery_worker` depend on successful migration completion
- `web` only handles collectstatic, optional admin bootstrap, and Gunicorn startup

## Known Architectural Limits

These are the main remaining gaps before the system can be called banking-grade:

1. `Account.balance` is still mutable state rather than a projection from immutable ledger entries.
2. PostgreSQL concurrency semantics are relied on in production, but the current default test lane uses SQLite.
3. Observability is still basic text logging rather than structured transaction event logs and traces.
4. There is no outbox pattern yet between transaction acceptance and broker publication.
5. Read scaling still relies on a single primary database with no replica routing or partitioning.

## Next Architecture Steps

Recommended priority order:

1. Add a PostgreSQL-backed integration test lane for idempotency and locking scenarios.
2. Introduce structured JSON logging for transaction lifecycle events.
3. Add Celery Beat or an external scheduler for periodic stale-transaction recovery.
4. Design immutable ledger entries and move `Account.balance` to a derived projection.
5. Introduce a release pipeline with explicit migrate, smoke test, and rollout phases.
