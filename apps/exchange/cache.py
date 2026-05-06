from decimal import Decimal

from django.conf import settings
from django.core.cache import cache

from core.cache_utils import (
    build_exchange_rate_cache_key,
    bump_exchange_rate_cache_version,
    get_exchange_rate_cache_version,
)

EXCHANGE_RATE_CACHE_NAMESPACE = "exchange_rate"


def get_cached_exchange_rate(
    *,
    provider: str,
    base_currency: str,
    quote_currency: str,
):
    """Return cached exchange rate."""
    version = get_exchange_rate_cache_version(
        namespace=EXCHANGE_RATE_CACHE_NAMESPACE,
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    cache_key = build_exchange_rate_cache_key(
        namespace=EXCHANGE_RATE_CACHE_NAMESPACE,
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
        version=version,
    )
    return cache.get(cache_key)


def set_cached_exchange_rate(
    *,
    provider: str,
    base_currency: str,
    quote_currency: str,
    rate: Decimal,
) -> Decimal:
    """Set cached exchange rate."""
    version = get_exchange_rate_cache_version(
        namespace=EXCHANGE_RATE_CACHE_NAMESPACE,
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    cache_key = build_exchange_rate_cache_key(
        namespace=EXCHANGE_RATE_CACHE_NAMESPACE,
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
        version=version,
    )
    cache.set(
        cache_key,
        rate,
        timeout=settings.EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS,
    )
    return rate


def invalidate_exchange_rate_cache(
    *,
    provider: str,
    base_currency: str,
    quote_currency: str,
) -> int:
    """Invalidate exchange rate cache."""
    return bump_exchange_rate_cache_version(
        namespace=EXCHANGE_RATE_CACHE_NAMESPACE,
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
