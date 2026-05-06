from django.contrib.auth import get_user_model
from django.db.models import Q

from core.db_routing import get_read_db_alias

from .models import Transaction

User = get_user_model()


def list_user_transactions(*, user: User, force_primary: bool = False):
    """List user transactions."""
    return (
        Transaction.objects.using(get_read_db_alias(force_primary=force_primary))
        .select_related("from_account", "to_account", "swift_details", "challenge")
        .filter(Q(from_account__owner=user) | Q(to_account__owner=user))
        .distinct()
    )
