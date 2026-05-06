from django.core.cache import cache


def get_user_cache_version(*, namespace: str, user_id: int) -> int:
    """Return user cache version."""
    cache_key = f"{namespace}:user:{user_id}:version"
    version = cache.get(cache_key)
    if version is None:
        cache.set(cache_key, 1)
        return 1
    return version


def bump_user_cache_version(*, namespace: str, user_id: int) -> int:
    """Bump user cache version."""
    cache_key = f"{namespace}:user:{user_id}:version"
    try:
        return cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 2)
        return 2


def build_user_cache_key(
    *,
    namespace: str,
    user_id: int,
    version: int,
    suffix: str,
) -> str:
    """Build user cache key."""
    return f"{namespace}:user:{user_id}:v{version}:{suffix}"


def get_account_cache_version(*, namespace: str, account_id: int) -> int:
    """Return account cache version."""
    cache_key = f"{namespace}:account:{account_id}:version"
    version = cache.get(cache_key)
    if version is None:
        cache.set(cache_key, 1)
        return 1
    return version


def bump_account_cache_version(*, namespace: str, account_id: int) -> int:
    """Bump account cache version."""
    cache_key = f"{namespace}:account:{account_id}:version"
    try:
        return cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 2)
        return 2


def build_account_cache_key(
    *,
    namespace: str,
    account_id: int,
    version: int,
    suffix: str,
) -> str:
    """Build account cache key."""
    return f"{namespace}:account:{account_id}:v{version}:{suffix}"


def get_exchange_rate_cache_version(
    *,
    namespace: str,
    provider: str,
    base_currency: str,
    quote_currency: str,
) -> int:
    """Return exchange rate cache version."""
    cache_key = (
        f"{namespace}:provider:{provider}:base:{base_currency}:quote:{quote_currency}:version"
    )
    version = cache.get(cache_key)
    if version is None:
        cache.set(cache_key, 1)
        return 1
    return version


def bump_exchange_rate_cache_version(
    *,
    namespace: str,
    provider: str,
    base_currency: str,
    quote_currency: str,
) -> int:
    """Bump exchange rate cache version."""
    cache_key = (
        f"{namespace}:provider:{provider}:base:{base_currency}:quote:{quote_currency}:version"
    )
    try:
        return cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 2)
        return 2


def build_exchange_rate_cache_key(
    *,
    namespace: str,
    provider: str,
    base_currency: str,
    quote_currency: str,
    version: int,
) -> str:
    """Build exchange rate cache key."""
    return (
        f"{namespace}:provider:{provider}:base:{base_currency}:quote:{quote_currency}:v{version}"
    )
