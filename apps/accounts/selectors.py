from django.contrib.auth import get_user_model

from .models import Account

User = get_user_model()


def list_user_accounts(*, user: User):
    """List user accounts."""
    return Account.objects.filter(owner=user).order_by("id")
