from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils.crypto import get_random_string
from rest_framework_simplejwt.tokens import RefreshToken

from .cache import refresh_account_balance_cache
from core.cache_utils import bump_user_cache_version

from .models import Account

User = get_user_model()
INITIAL_ACCOUNT_BALANCE = Decimal("1000.00")
SYSTEM_ACCOUNT_USERNAME = "micro-banking-system"


def generate_iban() -> str:
    """Handle generate iban."""
    while True:
        candidate = f"MB{get_random_string(30, allowed_chars='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ')}"
        if not Account.objects.filter(iban=candidate).exists():
            return candidate


def create_account_for_user(*, user: User, currency: str) -> Account:
    """Create account for user."""
    account = Account.objects.create(
        owner=user,
        currency=currency,
        iban=generate_iban(),
        balance=INITIAL_ACCOUNT_BALANCE,
    )
    refresh_account_balance_cache(account=account)
    bump_user_cache_version(namespace="accounts_list", user_id=user.id)
    return account


def get_or_create_system_account(*, currency: str) -> Account:
    """Return or create system account."""
    system_account = Account.objects.filter(
        currency=currency,
        is_system=True,
    ).first()
    if system_account is not None:
        return system_account

    system_user, _ = User.objects.get_or_create(
        username=SYSTEM_ACCOUNT_USERNAME,
        defaults={"email": ""},
    )
    if system_user.has_usable_password():
        system_user.set_unusable_password()
        system_user.save(update_fields=["password"])

    try:
        system_account = Account.objects.create(
            owner=system_user,
            currency=currency,
            iban=generate_iban(),
            balance=Decimal("0.00"),
            is_system=True,
        )
    except IntegrityError:
        system_account = Account.objects.get(
            currency=currency,
            is_system=True,
        )

    refresh_account_balance_cache(account=system_account)
    return system_account


def register_user(*, username: str, password: str, email: str = "") -> User:
    """Register user."""
    return User.objects.create_user(
        username=username,
        password=password,
        email=email,
    )


def build_auth_payload(*, user: User) -> dict:
    """Build auth payload."""
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
