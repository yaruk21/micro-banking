from django.conf import settings


def get_read_db_alias(*, force_primary: bool = False) -> str:
    """Return the database alias that should serve read queries."""

    if force_primary:
        return "default"

    if not getattr(settings, "READ_REPLICA_ENABLED", False):
        return "default"

    if "replica" not in settings.DATABASES:
        return "default"

    return "replica"
