from django.core.cache import cache


def get_user_cache_version(*, namespace: str, user_id: int) -> int:
    cache_key = f"{namespace}:user:{user_id}:version"
    version = cache.get(cache_key)
    if version is None:
        cache.set(cache_key, 1)
        return 1
    return version


def bump_user_cache_version(*, namespace: str, user_id: int) -> int:
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
    return f"{namespace}:user:{user_id}:v{version}:{suffix}"
