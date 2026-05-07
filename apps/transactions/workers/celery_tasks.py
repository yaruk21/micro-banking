import logging
from typing import Optional

from celery import shared_task
from django.conf import settings

from apps.transactions.application import (
    get_due_swift_transaction_ids,
    get_stuck_transaction_ids,
    mark_transaction_batch_failed,
    process_transaction_report,
    publish_pending_transaction_outbox,
    process_transaction_batch,
    process_transfer,
    process_swift_transfer,
)
from apps.transactions.partitioning import ensure_transaction_partitions
from core.logging_context import (
    reset_correlation_id,
    reset_task_id,
    set_correlation_id,
    set_task_id,
)
from core.structured_logging import log_event

logger = logging.getLogger("apps.transactions")


@shared_task(bind=True)
def publish_pending_transaction_outbox_task(self, limit: Optional[int] = None) -> int:
    """Publish pending transaction outbox task."""
    task_token = set_task_id(self.request.id)
    try:
        published_count = publish_pending_transaction_outbox(
            limit=limit or settings.TRANSACTION_OUTBOX_PUBLISH_BATCH_SIZE
        )
        if published_count:
            log_event(
                logger,
                logging.INFO,
                "transaction.outbox_publish_scheduled",
                message="Scheduled outbox publisher dispatched pending entries.",
                count=published_count,
                task_id=self.request.id,
            )
        return published_count
    finally:
        reset_task_id(task_token)


@shared_task(bind=True)
def dispatch_due_swift_transfers_task(self, limit: Optional[int] = None) -> int:
    """Dispatch due SWIFT transfers to the dedicated processing task."""
    task_token = set_task_id(self.request.id)
    try:
        transaction_ids = get_due_swift_transaction_ids(
            limit=limit or settings.SWIFT_TRANSFER_PICKUP_BATCH_SIZE
        )
        for transaction_id in transaction_ids:
            process_swift_transfer_task.delay(
                transaction_id,
                correlation_id=self.request.id,
            )

        if transaction_ids:
            log_event(
                logger,
                logging.INFO,
                "transaction.swift_due_dispatch_scheduled",
                message="Due SWIFT transfers were dispatched for processing.",
                transaction_ids=transaction_ids,
                count=len(transaction_ids),
                task_id=self.request.id,
            )
        return len(transaction_ids)
    finally:
        reset_task_id(task_token)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_transaction_batch_task(self, batch_id: int) -> None:
    """Process transaction batch task."""
    task_token = set_task_id(self.request.id)
    try:
        log_event(
            logger,
            logging.INFO,
            "transaction.batch_task_started",
            message="Transaction batch processing task started.",
            task_id=self.request.id,
        )
        batch = process_transaction_batch(batch_id=batch_id)
        log_event(
            logger,
            logging.INFO,
            "transaction.batch_task_finished",
            message="Transaction batch processing task finished.",
            task_id=self.request.id,
            status=batch.status,
            count=batch.total_items,
            succeeded_items=batch.succeeded_items,
            failed_items=batch.failed_items,
        )
    except Exception as exc:
        mark_transaction_batch_failed(batch_id=batch_id, reason=str(exc))
        raise
    finally:
        reset_task_id(task_token)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_transfer_task(
    self,
    transaction_id: int,
    correlation_id: Optional[str] = None,
) -> None:
    """Process transfer task."""
    correlation_token = set_correlation_id(correlation_id)
    task_token = set_task_id(self.request.id)
    try:
        log_event(
            logger,
            logging.INFO,
            "transaction.task_started",
            message="Transaction processing task started.",
            transaction_id=transaction_id,
            task_id=self.request.id,
        )
        transfer = process_transfer(transaction_id=transaction_id)
        log_event(
            logger,
            logging.INFO,
            "transaction.task_finished",
            message="Transaction processing task finished.",
            transaction_id=transaction_id,
            task_id=self.request.id,
            status=transfer.status,
            idempotency_key=transfer.idempotency_key,
        )
    finally:
        reset_task_id(task_token)
        reset_correlation_id(correlation_token)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_swift_transfer_task(
    self,
    transaction_id: int,
    correlation_id: Optional[str] = None,
) -> None:
    """Process one due SWIFT transfer."""
    correlation_token = set_correlation_id(correlation_id)
    task_token = set_task_id(self.request.id)
    try:
        log_event(
            logger,
            logging.INFO,
            "transaction.swift_task_started",
            message="SWIFT transaction processing task started.",
            transaction_id=transaction_id,
            task_id=self.request.id,
        )
        transfer = process_swift_transfer(transaction_id=transaction_id)
        log_event(
            logger,
            logging.INFO,
            "transaction.swift_task_finished",
            message="SWIFT transaction processing task finished.",
            transaction_id=transaction_id,
            task_id=self.request.id,
            status=transfer.status,
            idempotency_key=transfer.idempotency_key,
        )
    finally:
        reset_task_id(task_token)
        reset_correlation_id(correlation_token)


@shared_task(bind=True)
def generate_transaction_report_task(self, report_id: int) -> None:
    """Generate one transaction PDF report."""

    task_token = set_task_id(self.request.id)
    try:
        log_event(
            logger,
            logging.INFO,
            "transaction.report_task_started",
            message="Transaction report generation task started.",
            report_id=report_id,
            task_id=self.request.id,
        )
        report = process_transaction_report(report_id=report_id)
        log_event(
            logger,
            logging.INFO,
            "transaction.report_task_finished",
            message="Transaction report generation task finished.",
            report_id=report_id,
            task_id=self.request.id,
            status=report.status,
        )
    finally:
        reset_task_id(task_token)


@shared_task(bind=True)
def recover_stuck_transfers_task(self) -> int:
    """Recover stuck transfers task."""
    task_token = set_task_id(self.request.id)
    transaction_ids = get_stuck_transaction_ids(
        threshold_seconds=settings.TRANSACTION_STUCK_THRESHOLD_SECONDS
    )
    try:
        for transaction_id in transaction_ids:
            process_transfer_task.delay(transaction_id, correlation_id=self.request.id)

        if transaction_ids:
            log_event(
                logger,
                logging.WARNING,
                "transaction.requeued",
                message="Stuck transactions were requeued for processing.",
                transaction_ids=transaction_ids,
                count=len(transaction_ids),
                task_id=self.request.id,
            )
        return len(transaction_ids)
    finally:
        reset_task_id(task_token)


@shared_task(bind=True)
def ensure_transaction_partitions_task(self) -> int:
    """Ensure future monthly transaction partitions exist."""
    task_token = set_task_id(self.request.id)
    try:
        created_count = ensure_transaction_partitions(
            months_ahead=settings.TRANSACTION_PARTITION_MONTHS_AHEAD
        )
        log_event(
            logger,
            logging.INFO,
            "transaction.partitions_ensured",
            message="Scheduled transaction partition maintenance finished.",
            created_count=created_count,
            months_ahead=settings.TRANSACTION_PARTITION_MONTHS_AHEAD,
            task_id=self.request.id,
        )
        return created_count
    finally:
        reset_task_id(task_token)
