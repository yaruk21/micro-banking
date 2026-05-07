# Runbook

## Transaction Report PDF Download

### Purpose

This runbook covers the asynchronous transaction report flow:

- request report generation
- poll for completion
- download the generated PDF
- diagnose and recover from storage-related failures

### Expected Flow

1. `POST /api/transactions/reports/` returns `202 Accepted`
2. report row starts in `pending`
3. `celery_worker` moves it through `processing` to `completed`
4. `GET /api/transactions/reports/{id}/` returns a signed `download_url`
5. `GET /api/transactions/reports/{id}/download/?token=...` streams the PDF

### Required Runtime Assumptions

- `web` and `celery_worker` must use the same report storage
- with the default `FileSystemStorage`, both services must see the same `/app/media`
- in `docker-compose.ec2.yml` this is provided by the shared `transaction_report_media` volume
- with S3-compatible storage, both services must use the same bucket and credentials

### Common Symptoms

#### `409 Transaction report is not ready for download.`

Meaning:

- the report is still `pending` or `processing`
- or the completed artifact was not attached to the report record

What to check:

- call `GET /api/transactions/reports/{id}/`
- verify `status`
- inspect worker logs for report generation failures

#### `409 Transaction report file is unavailable.`

Meaning:

- PostgreSQL has a `completed` report row
- download authorization passed
- but the file at `storage_key` could not be opened from the active storage backend

Most common cause:

- `celery_worker` wrote the PDF to a filesystem location that `web` cannot see

Other possible causes:

- the file was deleted after generation
- the storage path changed between generation and download
- the report row points to a stale `storage_key`

### Quick Checks

#### 1. Inspect report metadata

Run in the `web` container:

```bash
docker compose -f docker-compose.ec2.yml exec web python manage.py shell
```

```python
from apps.transactions.models import TransactionReport
report = TransactionReport.objects.get(id=REPORT_ID)
print(report.status)
print(report.storage_key)
print(bool(report.pdf_content))
```

Expected:

- `status == "completed"`
- `storage_key` is non-empty for newly generated reports

#### 2. Verify that the file exists from `web`

```python
from django.core.files.storage import storages
storage = storages["transaction_reports"]
print(storage.exists(report.storage_key))
```

If this returns `False`, `web` cannot see the file.

#### 3. Verify that the file exists from `celery_worker`

```bash
docker compose -f docker-compose.ec2.yml exec celery_worker python manage.py shell
```

```python
from apps.transactions.models import TransactionReport
from django.core.files.storage import storages
report = TransactionReport.objects.get(id=REPORT_ID)
storage = storages["transaction_reports"]
print(storage.exists(report.storage_key))
```

Interpretation:

- `True` in worker and `False` in web means the services do not share the same storage
- `False` in both means the file was never persisted or was removed later

### Recovery

#### Case 1. Shared storage was misconfigured

Fix:

- ensure `web` and `celery_worker` share `/app/media`
- redeploy the stack

Current EC2 Compose expectation:

- shared volume: `transaction_report_media`

After the fix:

- create a new report
- old broken reports may still fail because their files were never recoverably stored

#### Case 3. Cloud storage rollout

Recommended production backend:

- `TRANSACTION_REPORT_STORAGE_BACKEND=core.storage_backends.S3PresignedReportStorage`

Required env:

- `TRANSACTION_REPORT_S3_BUCKET_NAME`
- `TRANSACTION_REPORT_S3_REGION_NAME`
- `TRANSACTION_REPORT_S3_ACCESS_KEY_ID`
- `TRANSACTION_REPORT_S3_SECRET_ACCESS_KEY`

Optional env:

- `TRANSACTION_REPORT_S3_ENDPOINT_URL`
- `TRANSACTION_REPORT_S3_ADDRESSING_STYLE`
- `TRANSACTION_REPORT_S3_SIGNATURE_VERSION`
- `TRANSACTION_REPORT_DOWNLOAD_URL_TTL_SECONDS`

Behavior:

- worker uploads the generated PDF into object storage
- `/api/transactions/reports/{id}/download/` redirects to a temporary pre-signed object URL
- clients still keep the same application endpoint flow

#### Case 2. One old report is broken

Fix:

- generate a new report for the same date range

Reason:

- the metadata row cannot reconstruct a missing filesystem artifact by itself

### Redeploy Checklist

```bash
docker compose -f docker-compose.ec2.yml up -d --build web celery_worker celery_beat
```

Then validate:

1. create a new report
2. poll until `completed`
3. open the returned `download_url`

### Notes

- always prefer the `download_url` returned by the status endpoint
- the signed token is temporary and intended for download only
- `pdf_content` exists only as a legacy fallback path for older report rows
