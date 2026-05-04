from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils.crypto import get_random_string
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Account

User = get_user_model()
INITIAL_ACCOUNT_BALANCE = Decimal("1000.00")


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
        balance=INITIAL_ACCOUNT_BALANCE,
    )


def register_user(*, username: str, password: str, email: str = "") -> User:
    return User.objects.create_user(
        username=username,
        password=password,
        email=email,
    )


def build_auth_payload(*, user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
        },
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }
