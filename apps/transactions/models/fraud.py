from django.conf import settings
from django.db import models


class FraudEvent(models.Model):
    """Stores user activity signals used by fraud and behavior analysis."""

    class EventType(models.TextChoices):
        """Supported fraud signal event types."""

        TRANSACTION_ATTEMPT = "transaction_attempt", "Transaction attempt"
        LOGIN = "login", "Login"
        CHALLENGE = "challenge", "Challenge"
        BLOCKED = "blocked", "Blocked"

    class Outcome(models.TextChoices):
        """Supported fraud evaluation outcomes."""

        ALLOWED = "allowed", "Allowed"
        FLAGGED = "flagged", "Flagged"
        BLOCKED = "blocked", "Blocked"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fraud_events",
    )
    transaction = models.ForeignKey(
        "transactions.Transaction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fraud_events",
        db_constraint=False,
    )
    request_id = models.CharField(max_length=255, blank=True, db_index=True)
    event_type = models.CharField(
        max_length=32,
        choices=EventType.choices,
        db_index=True,
    )
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        db_index=True,
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.CharField(max_length=512, blank=True)
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    region = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("user", "created_at"),
                name="fraud_evt_user_created_idx",
            ),
            models.Index(
                fields=("user", "event_type", "created_at"),
                name="fraud_evt_user_type_idx",
            ),
            models.Index(
                fields=("user", "outcome", "created_at"),
                name="fraud_evt_user_outcome_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        """Normalize canonical fraud-event fields before persisting."""

        self.request_id = self.request_id.strip()
        self.user_agent = self.user_agent.strip()
        self.country_code = self.country_code.upper().strip()
        self.region = self.region.strip()
        self.city = self.city.strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return a short human-readable representation of the fraud event."""

        return f"fraud:{self.user_id}:{self.event_type}:{self.outcome}"


class TransactionChallenge(models.Model):
    """Stores 2FA challenge state for suspicious or high-value transactions."""

    class Status(models.TextChoices):
        """Supported challenge states."""

        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transaction_challenges",
    )
    transaction = models.OneToOneField(
        "transactions.Transaction",
        on_delete=models.CASCADE,
        related_name="challenge",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    code_hash = models.CharField(max_length=255)
    reason_codes = models.CharField(max_length=255, blank=True)
    attempts_count = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    expires_at = models.DateTimeField(db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("user", "status", "created_at"),
                name="txn_challenge_user_status_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        """Normalize canonical challenge fields before persisting."""

        self.reason_codes = self.reason_codes.strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return a short human-readable representation of the challenge."""

        return f"txn-challenge:{self.transaction_id}:{self.status}"
