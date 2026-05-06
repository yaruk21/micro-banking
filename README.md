# Micro-Banking API

Clean and scalable Django REST API for user accounts and internal money transfers.

## Live demo

- Base URL: [http://51.21.197.200/](http://51.21.197.200/)
- Swagger UI: [http://51.21.197.200/api/docs/swagger/](http://51.21.197.200/api/docs/swagger/)
- OpenAPI schema: [http://51.21.197.200/api/schema/](http://51.21.197.200/api/schema/)
- Health check: [http://51.21.197.200/health/](http://51.21.197.200/health/)

## Project structure

```text
core/
apps/
  accounts/
  transactions/
```

Each app keeps:

- `models.py` for persistence
- `serializers.py` for input/output validation
- `views.py` for thin API endpoints
- `services.py` for business logic
- `selectors.py` for read-side queries

Architecture notes:

- [docs/architecture.md](/Users/macbook/Desktop/projects/Micro-Banking%20API/docs/architecture.md)

## Main decisions

- Django default `User` is enough for this scope
- transfer logic lives in `apps/transactions/services.py`
- race conditions are handled with `transaction.atomic()` + `select_for_update()`
- serializers stay lightweight and do not contain transfer logic
- PostgreSQL is the primary database for both Docker and local development
- `transactions_transaction` is partitioned by month in PostgreSQL using `created_at`
- Celery Beat automatically pre-creates future monthly transaction partitions
- JWT auth is provided by `djangorestframework-simplejwt`
- Redis is connected as the Celery broker/result backend
- Celery worker is ready for background tasks

## API endpoints

- `POST /api/register/`
- `POST /api/token/`
- `POST /api/token/refresh/`
- `GET /api/accounts/`
- `POST /api/accounts/`
- `GET /api/transactions/`
- `POST /api/transactions/`
- `GET /api/transactions/<id>/status/`
- `GET /api/schema/`
- `GET /api/docs/swagger/`
- `GET /api/docs/redoc/`

Transaction filters:

- `account=<account_id>`
- `date_from=2026-05-01`
- `date_to=2026-05-31`

## Example payloads

Register user:

```json
{
  "username": "alice",
  "email": "alice@example.com",
  "password": "StrongPass123!",
  "password_confirm": "StrongPass123!"
}
```

Login:

```json
{
  "username": "alice",
  "password": "StrongPass123!"
}
```

Create account:

```json
{
  "currency": "USD"
}
```

Newly created accounts receive an initial balance of `1000.00` for easier API testing.

Create transaction:

```json
{
  "from_account_iban": "MB123456789012345678901234567890",
  "to_account_iban": "MB098765432109876543210987654321",
  "amount": "25.00"
}
```

Required header for transaction creation:

```text
Idempotency-Key: transfer-001
```

## Typical flow

1. Register a new user via `POST /api/register/`.
2. Save the returned `access` and `refresh` tokens.
3. Use `Authorization: Bearer <access_token>` for protected endpoints.
4. Create one or more accounts with `POST /api/accounts/`.
5. Use returned `iban` values to create transfers with `POST /api/transactions/` and a required `Idempotency-Key` header.
6. Poll `GET /api/transactions/<id>/status/` until the status becomes `completed` or `failed`.
7. Use `POST /api/token/refresh/` when the access token expires.

## Notes

- New accounts receive an initial balance of `1000.00` to simplify API testing.
- Transfers are created using `from_account_iban` and `to_account_iban`.
- Transaction creation is asynchronous and returns `202 Accepted` for a new transfer.
- Repeating the same request with the same `Idempotency-Key` returns the existing transaction with `200 OK`.
- Reusing the same `Idempotency-Key` with a different payload returns `409 Conflict`.
- `Idempotency-Key` is required for `POST /api/transactions/`.
- Internal transfer logic still uses atomic database transactions and row locking.
- PostgreSQL keeps monthly transaction partitions plus a `DEFAULT` partition for safe write routing.
- A scheduled transaction partition maintenance task creates future monthly partitions ahead of time.

## Transaction creation example

```bash
curl -X POST http://localhost:8000/api/transactions/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: transfer-001" \
  -d '{
    "from_account_iban": "MB123456789012345678901234567890",
    "to_account_iban": "MB098765432109876543210987654321",
    "amount": "25.00"
  }'
```

Possible outcomes:

- `202 Accepted` for a newly queued transfer
- `200 OK` when the same request is replayed with the same `Idempotency-Key`
- `400 Bad Request` when `Idempotency-Key` is missing
- `409 Conflict` when the same `Idempotency-Key` is reused with a different payload
- `POST /api/transactions/batches/` for asynchronous batch submission of up to 1000 transactions
- `GET /api/transactions/batches/{id}/status/` for batch polling
- `GET /ws/transactions/{id}/?token=<access_token>` for realtime single-transaction status
- `GET /ws/transaction-batches/{id}/?token=<access_token>` for realtime batch status

## Local setup with .venv / venv

1. Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Windows:

```bash
.venv\Scripts\activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Prepare environment:

```bash
cp .env.example .env
```

Update `SECRET_KEY` in `.env` before real usage.

4. Make sure PostgreSQL is running locally and matches `.env` values.

If you plan to use Celery outside Docker, also start Redis locally and keep it available on `127.0.0.1:6379`.

If you want a quick local fallback without PostgreSQL for development checks only:

```bash
USE_SQLITE=1 python manage.py migrate
USE_SQLITE=1 python manage.py runserver
```

For PostgreSQL-backed transaction integration tests that exercise real locking and
idempotency races, keep PostgreSQL running and execute:

```bash
USE_POSTGRES_FOR_TESTS=1 ./.venv/bin/python -m pytest apps/transactions/tests/test_postgres_integration.py -q
```

If you need to retry accepted transactions that were persisted but not yet
dispatched to Celery, run:

```bash
python manage.py publish_transaction_outbox
```

5. Run migrations and create a user:

```bash
python manage.py migrate
python manage.py createsuperuser
```

6. Start the app:

```bash
python manage.py runserver
```

7. Start a Celery worker in a separate terminal:

```bash
celery -A core worker -l info
```

## Docker setup

```bash
docker compose up --build
```

This starts:

- `web` for the Django API
- `db` for PostgreSQL
- `redis` for Celery broker/backend
- `celery_worker` ready to process background tasks
- `celery_beat` to schedule outbox publishing and stale-transaction recovery

Local URLs:

- API: [http://localhost:8000](http://localhost:8000)
- Swagger UI: [http://localhost:8000/api/docs/swagger/](http://localhost:8000/api/docs/swagger/)
- Health check: [http://localhost:8000/health/](http://localhost:8000/health/)
- Redis: `localhost:6379`
- Celery worker runs as a separate `celery_worker` service
- Celery Beat runs as a separate `celery_beat` service

## EC2 deployment

This project is prepared for a simple EC2 deployment using Docker Compose.

Files for EC2:

- `docker-compose.ec2.yml`
- `.env.ec2.example`
- `deploy/entrypoint.sh`

Recommended EC2 setup:

1. Launch an Ubuntu EC2 instance.
2. Open Security Group ports:
   - `22` for SSH
   - `80` for HTTP
3. Install Docker and Docker Compose plugin.
4. Clone the repository on the instance.
5. Create the production env file:

```bash
cp .env.ec2.example .env.ec2
```

6. Update at least:
   - `SECRET_KEY`
   - `ALLOWED_HOSTS`
   - `CSRF_TRUSTED_ORIGINS`
   - `POSTGRES_PASSWORD`
   - `DJANGO_SUPERUSER_*`

7. Start the app:

```bash
docker compose -f docker-compose.ec2.yml --env-file .env.ec2 up -d --build
```

8. Verify deployment:

```bash
curl http://YOUR_EC2_PUBLIC_IP/health/
```

After deploy:

- API: `http://YOUR_EC2_PUBLIC_IP/api/accounts/`
- Swagger: `http://YOUR_EC2_PUBLIC_IP/api/docs/swagger/`
- Admin: `http://YOUR_EC2_PUBLIC_IP/admin/`

Current deployed EC2 instance:

- API: [http://51.21.197.200/api/accounts/](http://51.21.197.200/api/accounts/)
- Swagger: [http://51.21.197.200/api/docs/swagger/](http://51.21.197.200/api/docs/swagger/)
- Admin: [http://51.21.197.200/admin/](http://51.21.197.200/admin/)
