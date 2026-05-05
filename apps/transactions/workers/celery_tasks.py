import logging

from celery import shared_task
from django.conf import settings

from apps.transactions.application import (
    get_stuck_transaction_ids,
    process_transfer,
)

logger = logging.getLogger("apps.transactions")


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_transfer_task(transaction_id: int) -> None:
    process_transfer(transaction_id=transaction_id)


@shared_task
def recover_stuck_transfers_task() -> int:
    transaction_ids = get_stuck_transaction_ids(
        threshold_seconds=settings.TRANSACTION_STUCK_THRESHOLD_SECONDS
    )
    for transaction_id in transaction_ids:
        process_transfer_task.delay(transaction_id)

    if transaction_ids:
        logger.warning(
            "Requeued stuck transactions count=%s ids=%s",
            len(transaction_ids),
            transaction_ids,
        )
    return len(transaction_ids)
