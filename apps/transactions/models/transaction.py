from decimal import Decimal

from django.conf import settings
from django.db import models


class Transaction(models.Model):
    """Represents a single asynchronous transfer between two accounts."""

    class Status(models.TextChoices):
        """Supported processing states for a transaction."""

        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class TransferType(models.TextChoices):
        """Supported transfer rails."""

        INTERNAL = "internal", "Internal"
        SWIFT = "swift", "Swift"

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
        null=True,
        blank=True,
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
    transfer_type = models.CharField(
        max_length=10,
        choices=TransferType.choices,
        default=TransferType.INTERNAL,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("id",), name="txn_id_idx"),
            models.Index(fields=("status",), name="txn_status_idx"),
            models.Index(fields=("created_at",), name="txn_created_at_idx"),
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
            models.CheckConstraint(
                check=~models.Q(
                    transfer_type="internal",
                    to_account__isnull=True,
                ),
                name="txn_internal_requires_to_account",
            ),
        ]

    def __str__(self) -> str:
        """Return a short human-readable representation of the transaction."""

        recipient = self.to_account_id or "external"
        return (
            f"{self.from_account_id}->{recipient}:"
            f"{self.amount}/{self.credited_amount or self.amount}"
        )
