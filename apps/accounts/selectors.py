from django.contrib.auth import get_user_model

from core.db_routing import get_read_db_alias

from .models import Account

User = get_user_model()


def list_user_accounts(*, user: User, force_primary: bool = False):
    """List user accounts."""
    return (
        Account.objects.using(get_read_db_alias(force_primary=force_primary))
        .filter(owner=user)
        .order_by("id")
    )
