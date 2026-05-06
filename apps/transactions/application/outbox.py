import logging

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.transactions.models import Transaction, TransactionOutbox
from core.logging_context import get_correlation_id
from core.structured_logging import log_event

logger = logging.getLogger("apps.transactions")


def create_transaction_outbox(*, transaction: Transaction) -> TransactionOutbox:
    """Create transaction outbox."""
    outbox = TransactionOutbox.objects.create(
        transaction=transaction,
        correlation_id=get_correlation_id() or "",
    )
    log_event(
        logger,
        logging.INFO,
        "transaction.outbox_stored",
        message="Transaction outbox entry stored.",
        transaction_id=transaction.id,
        user_id=transaction.initiated_by_id,
        status=transaction.status,
        idempotency_key=transaction.idempotency_key,
    )
    return outbox


def register_transaction_outbox_publish(*, outbox_id: int) -> None:
    """Register transaction outbox publish."""
    db_transaction.on_commit(
        lambda: publish_transaction_outbox(outbox_id=outbox_id)
    )


def publish_transaction_outbox(*, outbox_id: int) -> bool:
    """Publish transaction outbox."""
    from apps.transactions.workers.celery_tasks import process_transfer_task

    with db_transaction.atomic():
        outbox = (
            TransactionOutbox.objects.select_for_update()
            .select_related("transaction")
            .filter(id=outbox_id)
            .first()
        )
        if outbox is None:
            return False

        if outbox.published_at is not None:
            return False

        try:
            async_result = process_transfer_task.delay(
                outbox.transaction_id,
                correlation_id=outbox.correlation_id or None,
            )
        except Exception as exc:
            outbox.delivery_attempts += 1
            outbox.last_error = str(exc)
            outbox.save(update_fields=["delivery_attempts", "last_error"])
            log_event(
                logger,
                logging.WARNING,
                "transaction.outbox_dispatch_failed",
                message="Failed to dispatch transaction from outbox.",
                transaction_id=outbox.transaction_id,
                user_id=outbox.transaction.initiated_by_id,
                status=outbox.transaction.status,
                idempotency_key=outbox.transaction.idempotency_key,
                failure_reason=outbox.last_error,
            )
            return False

        outbox.delivery_attempts += 1
        outbox.last_error = ""
        outbox.celery_task_id = async_result.id
        outbox.published_at = timezone.now()
        outbox.save(
            update_fields=[
                "delivery_attempts",
                "last_error",
                "celery_task_id",
                "published_at",
            ]
        )

    log_event(
        logger,
        logging.INFO,
        "transaction.outbox_dispatched",
        message="Transaction outbox entry dispatched to Celery.",
        transaction_id=outbox.transaction_id,
        user_id=outbox.transaction.initiated_by_id,
        status=outbox.transaction.status,
        idempotency_key=outbox.transaction.idempotency_key,
        task_id=outbox.celery_task_id,
    )
    return True


def get_pending_transaction_outbox_ids(*, limit: int = 100) -> list[int]:
    """Return pending transaction outbox ids."""
    return list(
        TransactionOutbox.objects.filter(published_at__isnull=True)
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:limit]
    )


def publish_pending_transaction_outbox(*, limit: int = 100) -> int:
    """Publish pending transaction outbox."""
    published_count = 0

    for outbox_id in get_pending_transaction_outbox_ids(limit=limit):
        if publish_transaction_outbox(outbox_id=outbox_id):
            published_count += 1

    if published_count:
        log_event(
            logger,
            logging.INFO,
            "transaction.outbox_batch_dispatched",
            message="Pending transaction outbox entries dispatched.",
            count=published_count,
        )
    return published_count
