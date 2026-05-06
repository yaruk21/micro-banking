from decimal import Decimal

from django.conf import settings
from django.core.cache import cache

from core.cache_utils import (
    build_account_cache_key,
    bump_account_cache_version,
    get_account_cache_version,
)

from .models import Account

ACCOUNT_BALANCE_CACHE_NAMESPACE = "account_balance"


def get_cached_account_balance(*, account: Account) -> Decimal:
    """Return cached account balance."""
    version = get_account_cache_version(
        namespace=ACCOUNT_BALANCE_CACHE_NAMESPACE,
        account_id=account.id,
    )
    cache_key = build_account_cache_key(
        namespace=ACCOUNT_BALANCE_CACHE_NAMESPACE,
        account_id=account.id,
        version=version,
        suffix="balance",
    )
    cached_balance = cache.get(cache_key)
    if cached_balance is not None:
        return cached_balance

    cache.set(
        cache_key,
        account.balance,
        timeout=settings.ACCOUNT_BALANCE_CACHE_TIMEOUT_SECONDS,
    )
    return account.balance


def refresh_account_balance_cache(*, account: Account) -> Decimal:
    """Refresh account balance cache."""
    version = bump_account_cache_version(
        namespace=ACCOUNT_BALANCE_CACHE_NAMESPACE,
        account_id=account.id,
    )
    cache_key = build_account_cache_key(
        namespace=ACCOUNT_BALANCE_CACHE_NAMESPACE,
        account_id=account.id,
        version=version,
        suffix="balance",
    )
    cache.set(
        cache_key,
        account.balance,
        timeout=settings.ACCOUNT_BALANCE_CACHE_TIMEOUT_SECONDS,
    )
    return account.balance
