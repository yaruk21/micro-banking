from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Transaction

User = get_user_model()


def list_user_transactions(*, user: User):
    return (
        Transaction.objects.select_related("from_account", "to_account")
        .filter(Q(from_account__owner=user) | Q(to_account__owner=user))
        .distinct()
    )
