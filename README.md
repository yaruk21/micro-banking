# Micro-Banking API

Clean and scalable Django REST API for user accounts and internal money transfers.

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

## Main decisions

- Django default `User` is enough for this scope
- transfer logic lives in `apps/transactions/services.py`
- race conditions are handled with `transaction.atomic()` + `select_for_update()`
- serializers stay lightweight and do not contain transfer logic
- PostgreSQL is the primary database for both Docker and local development
- JWT auth is provided by `djangorestframework-simplejwt`

## API endpoints

- `POST /api/token/`
- `POST /api/token/refresh/`
- `GET /api/accounts/`
- `POST /api/accounts/`
- `GET /api/transactions/`
- `POST /api/transactions/`
- `GET /api/schema/`
- `GET /api/docs/swagger/`
- `GET /api/docs/redoc/`

Transaction filters:

- `account=<account_id>`
- `date_from=2026-05-01`
- `date_to=2026-05-31`

## Example payloads

Create account:

```json
{
  "currency": "USD"
}
```

Create transaction:

```json
{
  "from_account": 1,
  "to_account": 2,
  "amount": "25.00"
}
```

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

If you want a quick local fallback without PostgreSQL for development checks only:

```bash
USE_SQLITE=1 python manage.py migrate
USE_SQLITE=1 python manage.py runserver
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

## Docker setup

```bash
docker compose up --build
```

The API will be available at [http://localhost:8000](http://localhost:8000).

Swagger UI will be available at
[http://localhost:8000/api/docs/swagger/](http://localhost:8000/api/docs/swagger/).
