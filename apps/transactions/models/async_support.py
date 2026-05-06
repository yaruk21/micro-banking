from django.conf import settings
from django.db import models


class TransactionOutbox(models.Model):
    """Tracks broker publication state for a transaction outbox entry."""

    transaction = models.OneToOneField(
        "transactions.Transaction",
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

    def __str__(self) -> str:
        """Return a short human-readable representation of the outbox row."""

        return f"outbox:{self.transaction_id}"


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

    def __str__(self) -> str:
        """Return a short human-readable representation of the registry row."""

        return (
            f"txn_idem:{self.initiated_by_id}:{self.idempotency_key}:"
            f"{self.transaction_id}"
        )
