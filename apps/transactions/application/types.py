from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model

User = get_user_model()


@dataclass(frozen=True)
class RequestFraudContext:
    """Represent request metadata used for fraud analysis."""

    request_id: str = ""
    ip_address: str = ""
    user_agent: str = ""
    country_code: str = ""
    region: str = ""
    city: str = ""
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None


@dataclass(frozen=True)
class TransferInput:
    """Represent transfer input."""
    user: User
    from_account_iban: str
    to_account_iban: str
    amount: Decimal
    idempotency_key: str
    fraud_context: Optional[RequestFraudContext] = None


@dataclass(frozen=True)
class SwiftTransferInput:
    """Represent SWIFT transfer input."""

    user: User
    from_account_iban: str
    amount: Decimal
    idempotency_key: str
    swift_code: str
    beneficiary_name: str
    beneficiary_account_number: str
    beneficiary_iban: str
    beneficiary_bank_name: str
    beneficiary_bank_country: str
    beneficiary_address: str
    swift_reference: str
    fraud_context: Optional[RequestFraudContext] = None


@dataclass(frozen=True)
class BatchTransferItemInput:
    """Represent batch transfer item input."""
    from_account_iban: str
    to_account_iban: str
    amount: Decimal
    idempotency_key: str
