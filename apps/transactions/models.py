from decimal import Decimal

from django.db import models


class Transaction(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    from_account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="outgoing_transactions",)
    to_account = models.ForeignKey("accounts.Account",on_delete=models.PROTECT, related_name="incoming_transactions", )
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=7, choices=Status.choices)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=Decimal("0.00")),
                name="transaction_amount_positive",
            )
        ]

    def __str__(self) -> str:
        return f"{self.from_account_id}->{self.to_account_id}:{self.amount}"
