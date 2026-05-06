from decimal import Decimal

from .cache import get_cached_exchange_rate, set_cached_exchange_rate
from .models import ExchangeRate


def get_exchange_rate(
    *,
    base_currency: str,
    quote_currency: str,
    provider: str = "privatbank",
) -> Decimal:
    """Return exchange rate."""
    if base_currency == quote_currency:
        return Decimal("1")

    cached_direct_rate = get_cached_exchange_rate(
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    if cached_direct_rate is not None:
        return cached_direct_rate

    direct_rate = (
        ExchangeRate.objects.filter(
            base_currency=base_currency,
            quote_currency=quote_currency,
            provider=provider,
        )
        .values_list("rate", flat=True)
        .first()
    )
    if direct_rate is not None:
        return set_cached_exchange_rate(
            provider=provider,
            base_currency=base_currency,
            quote_currency=quote_currency,
            rate=direct_rate,
        )

    if quote_currency == "UAH":
        raise ExchangeRate.DoesNotExist(
            f"Exchange rate {base_currency}->{quote_currency} does not exist."
        )

    base_to_uah = (
        ExchangeRate.objects.filter(
            base_currency=base_currency,
            quote_currency="UAH",
            provider=provider,
        )
        .values_list("rate", flat=True)
        .first()
    )

    quote_to_uah = (
        ExchangeRate.objects.filter(
            base_currency=quote_currency,
            quote_currency="UAH",
            provider=provider,
        )
        .values_list("rate", flat=True)
        .first()
    )

    if base_to_uah is None or quote_to_uah is None:
        raise ExchangeRate.DoesNotExist(
            f"Exchange rate {base_currency}->{quote_currency} does not exist."
        )

    cross_rate = base_to_uah / quote_to_uah
    return set_cached_exchange_rate(
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
        rate=cross_rate,
    )
