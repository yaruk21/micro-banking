from decimal import Decimal

from django.conf import settings
from django.db import models


# Stores one logical money movement, even though PostgreSQL may place the row in a monthly partition.
class Transaction(models.Model):
    """Represents a single asynchronous transfer between two accounts."""

    # Enumerates the lifecycle states used by async processing and polling.
    class Status(models.TextChoices):
        """Supported processing states for a transaction."""

        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    from_account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.PROTECT,
        related_name="outgoing_transactions",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="initiated_transactions",
    )
    to_account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.PROTECT,
        related_name="incoming_transactions",
    )
    idempotency_key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    credited_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=8,
        null=True,
        blank=True,
    )
    exchange_rate_provider = models.CharField(max_length=50, blank=True)
    fee_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    fee_currency = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)


    class Meta:
        """Represent meta."""
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("id",),
                name="txn_id_idx",
            ),
            models.Index(
                fields=("status",),
                name="txn_status_idx",
            ),
            models.Index(
                fields=("created_at",),
                name="txn_created_at_idx",
            ),
            models.Index(
                fields=("from_account", "created_at"),
                name="transaction_from_created_idx",
            ),
            models.Index(
                fields=("to_account", "created_at"),
                name="transaction_to_created_idx",
            ),
            models.Index(
                fields=("status", "created_at"),
                name="txn_status_created_idx",
            ),
            models.Index(
                fields=("status", "processing_started_at"),
                name="txn_status_proc_started_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=Decimal("0.00")),
                name="transaction_amount_positive",
            ),
        ]

    # Returns a compact admin/debug label for the transfer.
    def __str__(self) -> str:
        """Return a short human-readable representation of the transaction."""

        return (
            f"{self.from_account_id}->{self.to_account_id}:"
            f"{self.amount}/{self.credited_amount or self.amount}"
        )


# Stores a client-submitted async batch that fans out into multiple transfers.
class TransactionBatch(models.Model):
    """Represents a group of transfers submitted and tracked together."""

    # Reuses the same lifecycle pattern as single transactions for batch orchestration.
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
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        db_index=True,
    )
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

    # Returns a concise label for logs and debugging.
    def __str__(self) -> str:
        """Return a short human-readable representation of the batch."""

        return f"batch:{self.id}:{self.status}"


# Stores one input row inside an async batch submission.
class TransactionBatchItem(models.Model):
    """Represents a single queued item inside a transaction batch."""

    batch = models.ForeignKey(
        TransactionBatch,
        on_delete=models.CASCADE,
        related_name="items",
    )
    sequence = models.PositiveIntegerField()
    from_account_iban = models.CharField(max_length=34)
    to_account_iban = models.CharField(max_length=34)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    idempotency_key = models.CharField(max_length=255)
    transaction = models.ForeignKey(
        Transaction,
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

    # Returns a concise label for logs and debugging.
    def __str__(self) -> str:
        """Return a short human-readable representation of the batch item."""

        return f"batch_item:{self.batch_id}:{self.sequence}"


# Stores deferred Celery delivery metadata for accepted transactions.
class TransactionOutbox(models.Model):
    """Tracks broker publication state for a transaction outbox entry."""

    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.CASCADE,
        related_name="outbox",
        db_constraint=False,
    )
    correlation_id = models.CharField(max_length=255, blank=True)
    delivery_attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Represent meta."""
        ordering = ("created_at", "id")
        indexes = [
            models.Index(
                fields=("published_at", "created_at"),
                name="transaction_outbox_publish_idx",
            ),
        ]

    # Returns a concise label for logs and debugging.
    def __str__(self) -> str:
        """Return a short human-readable representation of the outbox row."""

        return f"outbox:{self.transaction_id}"


# Keeps global idempotency guarantees outside the partitioned transaction table.
class TransactionIdempotencyKey(models.Model):
    """Maps a user/idempotency key pair to the created transaction."""

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transaction_idempotency_keys",
    )
    idempotency_key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=255)
    transaction_id = models.BigIntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Represent meta."""
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("initiated_by", "idempotency_key"),
                name="txn_idem_registry_user_key_uniq",
            ),
        ]

    # Returns a concise label for logs and debugging.
    def __str__(self) -> str:
        """Return a short human-readable representation of the idempotency registry row."""

        return (
            f"txn_idem:{self.initiated_by_id}:{self.idempotency_key}:"
            f"{self.transaction_id}"
        )
