from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.db_routing import get_read_db_alias

from .models import Transaction

User = get_user_model()


def list_user_transactions(*, user: User, force_primary: bool = False):
    """List user transactions."""
    return (
        Transaction.objects.using(get_read_db_alias(force_primary=force_primary))
        .select_related("from_account", "to_account", "swift_details", "challenge")
        .filter(Q(from_account__owner=user) | Q(to_account__owner=user))
        .distinct()
    )


def list_user_transactions_for_period(
    *,
    user: User,
    date_from,
    date_to,
    force_primary: bool = False,
):
    """List user transactions within the selected created_at period."""

    return (
        Transaction.objects.using(get_read_db_alias(force_primary=force_primary))
        .select_related("from_account", "to_account")
        .filter(
            Q(from_account__owner=user) | Q(to_account__owner=user),
            **_build_created_at_filters(date_from=date_from, date_to=date_to),
        )
        .distinct()
    )


def summarize_user_transactions(
    *,
    user: User,
    date_from=None,
    date_to=None,
    force_primary: bool = False,
):
    """Return aggregated transaction analytics for one user."""

    read_db_alias = get_read_db_alias(force_primary=force_primary)
    date_filters = _build_created_at_filters(date_from=date_from, date_to=date_to)
    visible_transactions = Transaction.objects.using(read_db_alias).filter(
        Q(from_account__owner=user) | Q(to_account__owner=user),
        **date_filters,
    )
    completed_outgoing = Transaction.objects.using(read_db_alias).filter(
        from_account__owner=user,
        status=Transaction.Status.COMPLETED,
        **date_filters,
    )
    completed_incoming = Transaction.objects.using(read_db_alias).filter(
        to_account__owner=user,
        status=Transaction.Status.COMPLETED,
        **date_filters,
    )
    zero_amount = Value(Decimal("0.00"), output_field=DecimalField(max_digits=18, decimal_places=2))

    totals = visible_transactions.aggregate(
        total_transactions=Count("id"),
        pending_transactions=Count(
            "id",
            filter=Q(status=Transaction.Status.PENDING),
        ),
        processing_transactions=Count(
            "id",
            filter=Q(status=Transaction.Status.PROCESSING),
        ),
        completed_transactions=Count(
            "id",
            filter=Q(status=Transaction.Status.COMPLETED),
        ),
        failed_transactions=Count(
            "id",
            filter=Q(status=Transaction.Status.FAILED),
        ),
    )
    totals["completed_outgoing_amount"] = completed_outgoing.aggregate(
        value=Coalesce(Sum("amount"), zero_amount)
    )["value"]
    totals["completed_incoming_amount"] = completed_incoming.aggregate(
        value=Coalesce(
            Sum(Coalesce("credited_amount", F("amount"))),
            zero_amount,
        )
    )["value"]
    totals["net_completed_cashflow"] = (
        totals["completed_incoming_amount"] - totals["completed_outgoing_amount"]
    )

    currency_rows = {}
    for row in completed_outgoing.values(currency=F("from_account__currency")).annotate(
        outgoing_transactions=Count("id"),
        completed_outgoing_amount=Coalesce(Sum("amount"), zero_amount),
    ):
        currency_rows[row["currency"]] = {
            "currency": row["currency"],
            "outgoing_transactions": row["outgoing_transactions"],
            "incoming_transactions": 0,
            "completed_outgoing_amount": row["completed_outgoing_amount"],
            "completed_incoming_amount": Decimal("0.00"),
        }

    for row in completed_incoming.values(currency=F("to_account__currency")).annotate(
        incoming_transactions=Count("id"),
        completed_incoming_amount=Coalesce(
            Sum(Coalesce("credited_amount", F("amount"))),
            zero_amount,
        ),
    ):
        currency_summary = currency_rows.setdefault(
            row["currency"],
            {
                "currency": row["currency"],
                "outgoing_transactions": 0,
                "incoming_transactions": 0,
                "completed_outgoing_amount": Decimal("0.00"),
                "completed_incoming_amount": Decimal("0.00"),
            },
        )
        currency_summary["incoming_transactions"] = row["incoming_transactions"]
        currency_summary["completed_incoming_amount"] = row["completed_incoming_amount"]

    by_currency = sorted(currency_rows.values(), key=lambda item: item["currency"])
    for row in by_currency:
        row["net_completed_cashflow"] = (
            row["completed_incoming_amount"] - row["completed_outgoing_amount"]
        )

    by_transfer_type = list(
        visible_transactions.values("transfer_type")
        .annotate(
            total_transactions=Count("id"),
            completed_transactions=Count(
                "id",
                filter=Q(status=Transaction.Status.COMPLETED),
            ),
            total_amount=Coalesce(Sum("amount"), zero_amount),
        )
        .order_by("transfer_type")
    )

    return {
        "period": {
            "date_from": date_from,
            "date_to": date_to,
        },
        "totals": totals,
        "by_currency": by_currency,
        "by_transfer_type": by_transfer_type,
    }


def _build_created_at_filters(*, date_from=None, date_to=None):
    """Build inclusive date filters for created_at."""

    filters = {}
    current_timezone = timezone.get_current_timezone()

    if date_from is not None:
        filters["created_at__gte"] = timezone.make_aware(
            datetime.combine(date_from, time.min),
            current_timezone,
        )
    if date_to is not None:
        filters["created_at__lt"] = timezone.make_aware(
            datetime.combine(date_to + timedelta(days=1), time.min),
            current_timezone,
        )
    return filters
