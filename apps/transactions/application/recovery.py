from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.transactions.models import Transaction


def get_stuck_transaction_ids(*, threshold_seconds: int) -> list[int]:
    """Return stuck transaction ids."""
    threshold_time = timezone.now() - timedelta(seconds=threshold_seconds)
    return list(
        Transaction.objects.filter(
            transfer_type=Transaction.TransferType.INTERNAL,
        ).filter(
            Q(
                status=Transaction.Status.PENDING,
                created_at__lte=threshold_time,
            )
            | Q(
                status=Transaction.Status.PROCESSING,
                processing_started_at__isnull=False,
                processing_started_at__lte=threshold_time,
            )
        )
        .values_list("id", flat=True)
        .order_by("id")
    )
