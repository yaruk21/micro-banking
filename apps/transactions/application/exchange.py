from decimal import Decimal, ROUND_HALF_UP

from apps.accounts.models import Account
from apps.exchange.models import ExchangeRate
from apps.exchange.selectors import get_exchange_rate
from django.conf import settings

from .exceptions import TransactionValidationError

INTERNAL_EXCHANGE_RATE_PROVIDER = "internal"
EXTERNAL_EXCHANGE_RATE_PROVIDER = "privatbank"
RATE_PRECISION = Decimal("0.00000001")
AMOUNT_PRECISION = Decimal("0.01")
FX_EXCHANGE_FEE_RATE = Decimal(str(settings.FX_EXCHANGE_FEE_RATE))


def resolve_transfer_conversion(
    *,
    from_account: Account,
    to_account: Account,
    amount: Decimal,
) -> tuple[Decimal, Decimal, Decimal, str, str]:
    if from_account.currency == to_account.currency:
        return (
            Decimal("1.00000000"),
            amount.quantize(AMOUNT_PRECISION, rounding=ROUND_HALF_UP),
            Decimal("0.00"),
            to_account.currency,
            INTERNAL_EXCHANGE_RATE_PROVIDER,
        )

    try:
        exchange_rate = get_exchange_rate(
            base_currency=from_account.currency,
            quote_currency=to_account.currency,
            provider=EXTERNAL_EXCHANGE_RATE_PROVIDER,
        )
    except ExchangeRate.DoesNotExist as exc:
        raise TransactionValidationError(
            f"Exchange rate for {from_account.currency}->{to_account.currency} is not available."
        ) from exc

    normalized_exchange_rate = exchange_rate.quantize(
        RATE_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    gross_credited_amount = (amount * normalized_exchange_rate).quantize(
        AMOUNT_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    fee_amount = (gross_credited_amount * FX_EXCHANGE_FEE_RATE).quantize(
        AMOUNT_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    credited_amount = gross_credited_amount - fee_amount
    if credited_amount <= Decimal("0.00"):
        raise TransactionValidationError(
            "Converted amount must be greater than zero."
        )

    return (
        normalized_exchange_rate,
        credited_amount,
        fee_amount,
        to_account.currency,
        EXTERNAL_EXCHANGE_RATE_PROVIDER,
    )
