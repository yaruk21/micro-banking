import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.transactions.models import Transaction
from core.structured_logging import log_event

from .exceptions import TransactionLimitExceededError

logger = logging.getLogger("apps.transactions")
ZERO_AMOUNT = Decimal("0.00")


def enforce_transaction_limits(*, user, amount: Decimal) -> None:
    """Raise when a transaction would exceed configured single/day/month limits."""

    _enforce_single_transaction_limit(user=user, amount=amount)
    _enforce_period_limit(
        user=user,
        amount=amount,
        limit=settings.TRANSACTION_DAILY_LIMIT_AMOUNT,
        period="daily",
        period_start=_get_day_start(),
    )
    _enforce_period_limit(
        user=user,
        amount=amount,
        limit=settings.TRANSACTION_MONTHLY_LIMIT_AMOUNT,
        period="monthly",
        period_start=_get_month_start(),
    )


def _enforce_single_transaction_limit(*, user, amount: Decimal) -> None:
    """Validate the configured one-time transaction amount limit."""

    limit = settings.TRANSACTION_SINGLE_LIMIT_AMOUNT
    if limit <= ZERO_AMOUNT:
        return

    if amount <= limit:
        return

    raise _build_limit_error(
        user=user,
        amount=amount,
        period="single",
        limit=limit,
        current_total=ZERO_AMOUNT,
    )


def _enforce_period_limit(
    *,
    user,
    amount: Decimal,
    limit: Decimal,
    period: str,
    period_start,
) -> None:
    """Validate a rolling day or month transaction amount limit."""

    if limit <= ZERO_AMOUNT:
        return

    current_total = (
        Transaction.objects.filter(
            initiated_by=user,
            created_at__gte=period_start,
        )
        .exclude(status=Transaction.Status.FAILED)
        .aggregate(total=Sum("amount"))
        .get("total")
        or ZERO_AMOUNT
    )

    if current_total + amount <= limit:
        return

    raise _build_limit_error(
        user=user,
        amount=amount,
        period=period,
        limit=limit,
        current_total=current_total,
    )


def _build_limit_error(
    *,
    user,
    amount: Decimal,
    period: str,
    limit: Decimal,
    current_total: Decimal,
) -> TransactionLimitExceededError:
    """Build and log a limit-exceeded error."""

    period_labels = {
        "single": "single",
        "daily": "daily",
        "monthly": "monthly",
    }
    message = (
        f"The {period_labels[period]} transaction limit is exceeded."
    )
    log_event(
        logger,
        logging.WARNING,
        "transaction.limit_exceeded",
        message="Transaction rejected because a configured amount limit was exceeded.",
        user_id=user.id,
        amount=amount,
        failure_reason=(
            f"{period} limit exceeded: current_total={current_total}, "
            f"requested_amount={amount}, limit={limit}"
        ),
    )
    return TransactionLimitExceededError(message)


def _get_day_start():
    """Return the current day start in the active timezone."""

    now = timezone.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_month_start():
    """Return the current month start in the active timezone."""

    now = timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
