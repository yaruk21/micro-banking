import hashlib
from decimal import Decimal

from .exceptions import IdempotencyConflictError


# Builds a deterministic hash for replay-safe transfer creation.
def build_transfer_fingerprint(
    *,
    from_account_iban: str,
    to_account_iban: str,
    amount: Decimal,
) -> str:
    """Return a stable fingerprint for a transfer payload."""

    normalized_amount = format(amount.quantize(Decimal("0.01")), "f")
    raw_value = f"{from_account_iban}:{to_account_iban}:{normalized_amount}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def build_swift_transfer_fingerprint(
    *,
    from_account_iban: str,
    amount: Decimal,
    swift_code: str,
    beneficiary_name: str,
    beneficiary_account_number: str,
    beneficiary_iban: str,
    beneficiary_bank_name: str,
    beneficiary_bank_country: str,
    beneficiary_address: str,
    swift_reference: str,
) -> str:
    """Return a stable fingerprint for a SWIFT transfer payload."""

    normalized_amount = format(amount.quantize(Decimal("0.01")), "f")
    parts = [
        from_account_iban.strip().upper(),
        normalized_amount,
        swift_code.replace(" ", "").upper().strip(),
        beneficiary_name.strip(),
        beneficiary_account_number.replace(" ", "").upper().strip(),
        beneficiary_iban.replace(" ", "").upper().strip(),
        beneficiary_bank_name.strip(),
        beneficiary_bank_country.upper().strip(),
        beneficiary_address.strip(),
        swift_reference.strip(),
    ]
    raw_value = ":".join(parts)
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# Verifies that a reused idempotency key still points to the same logical payload.
def validate_request_fingerprint(
    *,
    existing_request_fingerprint: str,
    request_fingerprint: str,
) -> None:
    """Raise when a reused idempotency key has a different payload fingerprint."""

    if existing_request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            "This Idempotency-Key is already used for a different transaction payload."
        )
