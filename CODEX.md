# CODEX Project Context

## 1. Project overview

- **What this project is about:** a Django REST API for micro-banking operations: user registration, account management, internal money transfers, FX conversion, async batch transfers, and transaction status tracking.
- **Main goal of the application:** provide a clean monolith that already includes several high-load building blocks: asynchronous transaction intake, idempotency, outbox-based dispatch, Celery workers, Redis-backed caching/realtime updates, PostgreSQL partitioning, and optional read-replica routing.
- **Current tech stack:** Python 3.10, Django 4.2, Django REST Framework, SimpleJWT, django-filter, drf-spectacular, PostgreSQL, Redis, Celery, Gunicorn, WhiteNoise, pytest, Docker Compose.
- **Architecture style:** modular Django monolith with domain apps in `apps/`, thin API views, service/application functions for business logic, and ORM-based persistence.

## 2. Project structure

- `core/`: global settings, URL wiring, ASGI/WSGI, Celery bootstrap, DB runtime tuning, read-replica helper, cache helpers, structured logging, health check.
- `apps/accounts/`: account model, registration serializer/view, account list/create API, account caching, small account services/selectors.
- `apps/transactions/`: main transaction domain.
- `apps/transactions/models/`: split transaction domain models package. Models are grouped by concern (`transaction`, `batch`, `async_support`, `swift`, `fraud`) and re-exported through `apps.transactions.models`.
- `apps/transactions/api/`: REST serializers, views, filters, URL routes.
- `apps/transactions/application/`: transaction creation, processing, idempotency, batch orchestration, FX resolution, outbox, recovery helpers, input dataclasses.
- `apps/transactions/workers/`: Celery tasks for processing, recovery, outbox publishing, partition maintenance.
- `apps/transactions/management/commands/`: operational commands for outbox publish and stuck-transfer recovery.
- `apps/exchange/`: exchange-rate model, selectors, sync services, Redis cache, PrivatBank integration, Celery sync task.
- `docs/architecture.md`: current architecture notes and known limits.
- `deploy/`: dev/prod entrypoints and PgBouncer container files.
- `docker-compose.yml`: local dev stack.
- `docker-compose.ec2.yml`: production-like Compose topology with PgBouncer and one-shot migration service.
- `tests`: there is no single top-level `tests/` folder; tests live next to apps, mainly under `apps/transactions/tests/`, `apps/exchange/tests/`, `apps/accounts/tests.py`, and `core/tests/`.

## 3. Architecture and conventions

- **API router wiring:** `core/urls.py` mounts:
  - `/api/register/`
  - `/api/token/`, `/api/token/refresh/`
  - `/api/accounts/`
  - `/api/transactions/`
  - `/api/schema/`, `/api/docs/swagger/`, `/api/docs/redoc/`
- **Realtime wiring:** `core/asgi.py` sends WebSocket scopes to `core/websocket_status.py`. Current WS endpoints are `/ws/transactions/<id>/` and `/ws/transaction-batches/<id>/`.
- **Database/session handling:** plain Django ORM; consistency-sensitive flows use `transaction.atomic()` and `select_for_update()`. Read-side routing goes through `core.db_routing.get_read_db_alias()` and is used in selectors.
- **Where business logic should live:**
  - keep REST views thin
  - put reusable read queries in selectors
  - keep small/simple domain helpers in `services.py`
  - put transaction-heavy orchestration in `apps/transactions/application/`
- **Current conventions already used:**
  - keyword-only service functions are common
  - `TextChoices` enums for statuses/currencies/transfer types
  - serializers validate shape/basic rules; they do not contain core transfer logic
  - caching uses explicit versioned keys from `core/cache_utils.py`
  - structured JSON logs go through `core.structured_logging.log_event()`
  - comments/docstrings are short and practical

## 4. Database

- **Migration system:** Django migrations only. No Alembic in the repo.
- **Accounts:**
  - `Account`: `owner`, `iban`, `balance`, `currency`, `is_system`, `created_at`
  - important constraints: non-negative balance, unique `iban`, unique system account per currency
- **Transactions:**
  - `Transaction`: `from_account`, `to_account`, `initiated_by`, `idempotency_key`, `request_fingerprint`, `amount`, `credited_amount`, `exchange_rate`, `fee_amount`, `status`, `transfer_type`, timestamps, `failure_reason`
  - lifecycle: `pending -> processing -> completed/failed`
  - current transfer types: `internal`, `swift`
- **Batching:**
  - `TransactionBatch`: batch request envelope and counters
  - `TransactionBatchItem`: one queued item, optional linked `Transaction`, item-level error text
- **Reliability/supporting tables:**
  - `TransactionOutbox`: async dispatch state for accepted transfers
  - `TransactionIdempotencyKey`: separate registry for global idempotency outside the partitioned transaction table
  - `SwiftTransferDetails`: SWIFT recipient/fee/scheduling metadata for one transaction
  - `FraudEvent`: user activity/risk signal storage for fraud frequency and geolocation analysis
- **FX:**
  - `ExchangeRate`: `base_currency`, `quote_currency`, `rate`, `provider`, `fetched_at`
- **Relationships:**
  - `User -> Account` one-to-many
  - `User -> initiated Transaction/TransactionBatch/TransactionIdempotencyKey` one-to-many
  - `Account -> outgoing/incoming Transaction` one-to-many
  - `Transaction -> TransactionOutbox` one-to-one
  - `Transaction -> SwiftTransferDetails` one-to-one
  - `TransactionBatch -> TransactionBatchItem` one-to-many
- **Indexes/partitioning:**
  - transaction indexes exist for `status`, `created_at`, `from_account+created_at`, `to_account+created_at`, `status+created_at`, `status+processing_started_at`
  - `transactions_transaction` is range-partitioned by month on `created_at` in PostgreSQL, with a `DEFAULT` partition
  - partition helpers live in `apps/transactions/partitioning.py`
- **Current migration progression worth knowing:**
  - async fields/idempotency were added before outbox
  - batch tables were added in `0005`
  - status/time indexes in `0008`
  - standalone idempotency registry in `0009`
  - monthly partitioning in `0010`
  - SWIFT details and transfer-type support in `0011`/`0012`

## 5. Authentication and users

- Uses Django’s default user model; there is no custom auth model.
- Registration is handled by `apps/accounts/views.py -> RegisterView` and `RegisterSerializer`.
- `register_user()` uses `User.objects.create_user(...)`, so password hashing is Django’s standard password hasher stack.
- Login is handled by SimpleJWT at `/api/token/`; refresh is `/api/token/refresh/`.
- Registration returns a JWT payload immediately via `build_auth_payload()`.
- Global DRF auth is JWT (`rest_framework_simplejwt.authentication.JWTAuthentication`).
- Global default permission is `IsAuthenticated`; registration explicitly uses `AllowAny`.
- WebSockets also authenticate JWT, either from `?token=...` or `Authorization: Bearer ...`, inside `core/websocket_status.py`.
- There is no separate custom “auth dependency” layer; auth behavior is mostly in `core/settings.py`, `apps/accounts/views.py`, and websocket helpers.

## 6. Current implemented features

- **Implemented now:**
  - user registration
  - JWT login/refresh
  - account creation/listing
  - cached account balances
  - internal async transfer creation with required `Idempotency-Key`
  - transaction polling endpoint
  - batch transfer submission up to 1000 items
  - batch status polling
  - WebSocket realtime status for single transfers and batches
  - FX conversion for internal transfers using cached/stored exchange rates
  - FX fee routing into per-currency system accounts
  - outbox-based Celery dispatch and retry/recovery path for stuck internal transfers
  - monthly PostgreSQL partition maintenance
  - read-replica-aware selectors
  - structured JSON logging with propagated request/task correlation ids
  - DRF throttling scopes for register/accounts/transactions
  - configurable single/day/month transaction amount limits for internal and SWIFT create flows
- **Partially implemented / active area:**
  - SWIFT transfer creation endpoint exists and stores beneficiary metadata plus planned timestamps
  - SWIFT status is exposed in serializers/status payloads
  - fraud detection is started: transaction amount limits are implemented, and `FraudEvent` schema now exists for behavior analysis
- **Clearly not implemented yet (based on current repo):**
  - anomaly-frequency and geolocation fraud rules are not implemented yet
  - 2FA challenge flow is not implemented yet
  - analytics/reporting endpoints
  - PDF report generation
  - cloud storage + temporary file links
  - dashboards/tracing infrastructure
  - immutable ledger accounting
  - multi-region AWS setup in code
  - CI/CD pipeline config in repo is unclear/not present
  - pagination for list endpoints is not configured

## 7. Development workflow

- **Local Python setup:**
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env
  ```
- **Run with Docker Compose:**
  ```bash
  docker compose up --build
  ```
  Services: `db`, `redis`, `web`, `celery_worker`, `celery_beat`
- **Run without Docker:**
  ```bash
  python manage.py migrate
  python manage.py runserver
  celery -A core worker -l info
  celery -A core beat -l info --schedule /tmp/celerybeat-schedule
  ```
- **Useful env vars (names only):**
  - Django/core: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `TIME_ZONE`
  - DB: `USE_SQLITE`, `USE_POSTGRES_FOR_TESTS`, `POSTGRES_*`, `POSTGRES_REPLICA_*`, `READ_REPLICA_ENABLED`
  - Redis/Celery: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `REDIS_CACHE_URL`, `REALTIME_REDIS_URL`
  - pooling/tuning: `DB_USE_PGBOUNCER`, `CONN_MAX_AGE`, `DB_CONN_HEALTH_CHECKS`, `DB_DISABLE_SERVER_SIDE_CURSORS`
  - app tuning: `LIST_CACHE_TIMEOUT_SECONDS`, `ACCOUNT_BALANCE_CACHE_TIMEOUT_SECONDS`, `EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS`, `TRANSACTION_STUCK_THRESHOLD_SECONDS`, `TRANSACTION_OUTBOX_PUBLISH_*`, `TRANSACTION_PARTITION_*`, `EXCHANGE_RATE_SYNC_INTERVAL_SECONDS`, `FX_EXCHANGE_FEE_RATE`, `TRANSACTION_SINGLE_LIMIT_AMOUNT`, `TRANSACTION_DAILY_LIMIT_AMOUNT`, `TRANSACTION_MONTHLY_LIMIT_AMOUNT`
- **Migrations:** use Django migrations via `python manage.py migrate`.
- **Operational commands:**
  ```bash
  python manage.py publish_transaction_outbox --limit 100
  python manage.py recover_stuck_transfers
  ```
- **Tests:**
  ```bash
  pytest
  ```
  Default pytest path uses SQLite unless `USE_POSTGRES_FOR_TESTS=1`.
- **PostgreSQL-only integration tests:**
  ```bash
  USE_POSTGRES_FOR_TESTS=1 ./.venv/bin/python -m pytest apps/transactions/tests/test_postgres_integration.py -q
  ```

## 8. Rules for future coding tasks

- Act as: senior backend dev, code reviewer,performance engineer
- Prefer simple, clean, idiomatic Django/DRF code.
- Follow DRY, SOLID, and KISS when they help; do not force extra abstractions.
- Keep routers/views thin.
- Put business logic in services/application functions only when it materially improves clarity/reuse.
- Reuse selectors for read-side queries.
- Use efficient ORM queries, `select_related`/`prefetch_related` where needed, and avoid N+1 patterns.
- Preserve idempotency, cache invalidation, and locking semantics on transaction changes.
- Be careful with read-replica routing: status/read-after-write paths may need primary reads.
- Add comments/docstrings only for non-obvious logic.
- Do not change unrelated files.
- Do not rewrite architecture without asking first.
- Before coding, briefly explain which files will change.
- Treat the current transaction system as async-first: accepted requests should usually persist state first, then dispatch background work safely.
