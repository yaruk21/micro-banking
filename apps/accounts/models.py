from decimal import Decimal

from django.conf import settings
from django.db import models


class Account(models.Model):
    class Currency(models.TextChoices):
        USD = "USD", "USD"
        EUR = "EUR", "EUR"
        UAH = "UAH", "UAH"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="accounts",)
    iban = models.CharField(max_length=34, unique=True, db_index=True)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, choices=Currency.choices)
    is_system = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(
                check=models.Q(balance__gte=0),
                name="account_balance_non_negative",
            ),
            models.UniqueConstraint(
                fields=("currency",),
                condition=models.Q(is_system=True),
                name="unique_system_account_per_currency",
            ),
        ]

    def __str__(self) -> str:
        system_label = " system" if self.is_system else ""
        return f"{self.iban} ({self.currency}{system_label})"

    @property
    def cached_balance(self) -> Decimal:
        from .cache import get_cached_account_balance

        return get_cached_account_balance(account=self)
