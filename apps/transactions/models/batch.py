from django.conf import settings
from django.db import models


class TransactionBatch(models.Model):
    """Represents a group of transfers submitted and tracked together."""

    class Status(models.TextChoices):
        """Supported processing states for a transaction batch."""

        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="transaction_batches",
    )
    idempotency_key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    total_items = models.PositiveIntegerField(default=0)
    processed_items = models.PositiveIntegerField(default=0)
    succeeded_items = models.PositiveIntegerField(default=0)
    failed_items = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("initiated_by", "idempotency_key"),
                name="txn_batch_user_idem_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Return a short human-readable representation of the batch."""

        return f"batch:{self.id}:{self.status}"


class TransactionBatchItem(models.Model):
    """Represents a single queued item inside a transaction batch."""

    batch = models.ForeignKey(
        "transactions.TransactionBatch",
        on_delete=models.CASCADE,
        related_name="items",
    )
    sequence = models.PositiveIntegerField()
    from_account_iban = models.CharField(max_length=34)
    to_account_iban = models.CharField(max_length=34)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    idempotency_key = models.CharField(max_length=255)
    transaction = models.ForeignKey(
        "transactions.Transaction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="batch_items",
        db_constraint=False,
    )
    created_transaction = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Represent meta."""

        ordering = ("sequence", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "sequence"),
                name="txn_batch_item_seq_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("batch", "sequence"),
                name="txn_batch_item_seq_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a short human-readable representation of the batch item."""

        return f"batch_item:{self.batch_id}:{self.sequence}"
