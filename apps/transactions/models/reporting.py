from django.conf import settings
from django.db import models


class TransactionReport(models.Model):
    """Stores one asynchronously generated PDF report for a user."""

    class Status(models.TextChoices):
        """Supported report generation states."""

        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transaction_reports",
    )
    date_from = models.DateField()
    date_to = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    file_name = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(
        max_length=100,
        blank=True,
        default="application/pdf",
    )
    storage_key = models.CharField(max_length=500, blank=True)
    pdf_content = models.BinaryField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("user", "status", "created_at"),
                name="txn_report_user_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(date_to__gte=models.F("date_from")),
                name="txn_report_date_range_valid",
            ),
        ]

    def __str__(self) -> str:
        """Return a short human-readable representation of the report."""

        return (
            f"txn-report:{self.user_id}:{self.date_from}:{self.date_to}:{self.status}"
        )
