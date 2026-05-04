from django.contrib.auth import get_user_model
from django.utils.crypto import get_random_string

from .models import Account

User = get_user_model()


def generate_iban() -> str:
    while True:
        candidate = f"MB{get_random_string(30, allowed_chars='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ')}"
        if not Account.objects.filter(iban=candidate).exists():
            return candidate


def create_account_for_user(*, user: User, currency: str) -> Account:
    return Account.objects.create(
        owner=user,
        currency=currency,
        iban=generate_iban(),
    )
