from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model

User = get_user_model()


@dataclass(frozen=True)
class TransferInput:
    user: User
    from_account_iban: str
    to_account_iban: str
    amount: Decimal
    idempotency_key: str
