import hashlib
from decimal import Decimal

from apps.transactions.models import Transaction

from .exceptions import IdempotencyConflictError


def build_transfer_fingerprint(
    *,
    from_account_iban: str,
    to_account_iban: str,
    amount: Decimal,
) -> str:
    normalized_amount = format(amount.quantize(Decimal("0.01")), "f")
    raw_value = f"{from_account_iban}:{to_account_iban}:{normalized_amount}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def validate_request_fingerprint(
    *,
    transfer: Transaction,
    request_fingerprint: str,
) -> None:
    if transfer.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            "This Idempotency-Key is already used for a different transaction payload."
        )
