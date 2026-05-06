
from django.db import models
from apps.accounts.models import Account


class ExchangeRate(models.Model):
    """Represent exchange rate."""
    base_currency = models.CharField(max_length=3, choices=Account.Currency.choices, db_index=True)
    quote_currency = models.CharField(max_length=3, choices=Account.Currency.choices, db_index=True)
    rate = models.DecimalField(max_digits=20, decimal_places=8)
    provider = models.CharField(max_length=50, default="manual", db_index=True)
    fetched_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Represent meta."""
        constraints = [
            models.UniqueConstraint(
                fields=["base_currency", "quote_currency", "provider"],
                name="unique_exchange_rate_per_provider_pair",
            ),
            models.CheckConstraint(
                check=~models.Q(base_currency=models.F("quote_currency")),
                name="exchange_rate_currencies_must_differ",
            ),
            models.CheckConstraint(
                check=models.Q(rate__gt=0),
                name="exchange_rate_must_be_positive",
            ),
        ]

