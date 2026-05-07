from __future__ import annotations

from io import BytesIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import File
from django.core.files.storage import Storage


class S3PresignedReportStorage(Storage):
    """Store transaction reports in S3-compatible object storage."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bucket_name = settings.TRANSACTION_REPORT_S3_BUCKET_NAME
        self.region_name = settings.TRANSACTION_REPORT_S3_REGION_NAME
        self.endpoint_url = settings.TRANSACTION_REPORT_S3_ENDPOINT_URL
        self.access_key_id = settings.TRANSACTION_REPORT_S3_ACCESS_KEY_ID
        self.secret_access_key = settings.TRANSACTION_REPORT_S3_SECRET_ACCESS_KEY
        self.addressing_style = settings.TRANSACTION_REPORT_S3_ADDRESSING_STYLE
        self.signature_version = settings.TRANSACTION_REPORT_S3_SIGNATURE_VERSION
        self._client = None
        if not self.bucket_name:
            raise ImproperlyConfigured(
                "TRANSACTION_REPORT_S3_BUCKET_NAME must be set for S3 report storage."
            )

    @property
    def client(self):
        """Build and memoize the boto3 client lazily."""

        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImproperlyConfigured(
                    "boto3 must be installed to use S3PresignedReportStorage."
                ) from exc

            session = boto3.session.Session()
            client_kwargs = {
                "service_name": "s3",
                "region_name": self.region_name or None,
                "endpoint_url": self.endpoint_url or None,
                "aws_access_key_id": self.access_key_id or None,
                "aws_secret_access_key": self.secret_access_key or None,
            }
            if self.signature_version or self.addressing_style:
                from botocore.config import Config

                client_kwargs["config"] = Config(
                    signature_version=self.signature_version or None,
                    s3=(
                        {"addressing_style": self.addressing_style}
                        if self.addressing_style
                        else None
                    ),
                )
            self._client = session.client(**client_kwargs)
        return self._client

    def _open(self, name, mode="rb"):
        """Download one object and expose it as a Django file."""

        buffer = BytesIO()
        self.client.download_fileobj(self.bucket_name, name, buffer)
        buffer.seek(0)
        return File(buffer, name=name)

    def _save(self, name, content):
        """Upload one object to S3-compatible storage."""

        content.open("rb")
        self.client.upload_fileobj(
            content.file,
            self.bucket_name,
            name,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        return name

    def delete(self, name):
        """Delete one object if it exists."""

        if not name:
            return
        self.client.delete_object(Bucket=self.bucket_name, Key=name)

    def exists(self, name):
        """Return whether one object exists."""

        try:
            self.client.head_object(Bucket=self.bucket_name, Key=name)
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def url(self, name, parameters=None, expire=None):
        """Return one pre-signed GET URL."""

        if not name:
            return ""
        params = {
            "Bucket": self.bucket_name,
            "Key": name,
        }
        if parameters:
            params.update(parameters)
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expire or settings.TRANSACTION_REPORT_DOWNLOAD_URL_TTL_SECONDS,
        )
