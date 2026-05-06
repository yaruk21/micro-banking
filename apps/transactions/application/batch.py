import hashlib
import logging
from dataclasses import asdict

from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone

from apps.transactions.models import (
    TransactionBatch,
    TransactionBatchItem,
)
from apps.transactions.realtime import publish_transaction_batch_status_update
from core.structured_logging import log_event

from .create import create_transfer
from .exceptions import (
    IdempotencyConflictError,
    TransactionPermissionError,
    TransactionValidationError,
)
from .types import BatchTransferItemInput, TransferInput, User

logger = logging.getLogger("apps.transactions")


def create_transaction_batch(
    *,
    user: User,
    idempotency_key: str,
    items: list[BatchTransferItemInput],
) -> tuple[TransactionBatch, bool]:
    """Create transaction batch."""
    if not idempotency_key.strip():
        raise TransactionValidationError("Idempotency-Key header is required.")

    if not items:
        raise TransactionValidationError("Batch must contain at least one transaction.")

    effective_idempotency_key = idempotency_key.strip()
    request_fingerprint = _build_batch_fingerprint(items=items)

    existing_batch = TransactionBatch.objects.filter(
        initiated_by=user,
        idempotency_key=effective_idempotency_key,
    ).first()
    if existing_batch is not None:
        _validate_batch_request_fingerprint(
            batch=existing_batch,
            request_fingerprint=request_fingerprint,
        )
        _log_replayed_batch(existing_batch)
        return existing_batch, False

    try:
        with db_transaction.atomic():
            batch = TransactionBatch.objects.create(
                initiated_by=user,
                idempotency_key=effective_idempotency_key,
                request_fingerprint=request_fingerprint,
                status=TransactionBatch.Status.PENDING,
                total_items=len(items),
            )
            TransactionBatchItem.objects.bulk_create(
                [
                    TransactionBatchItem(
                        batch=batch,
                        sequence=index,
                        from_account_iban=item.from_account_iban,
                        to_account_iban=item.to_account_iban,
                        amount=item.amount,
                        idempotency_key=item.idempotency_key,
                    )
                    for index, item in enumerate(items, start=1)
                ]
            )
            db_transaction.on_commit(
                lambda: _dispatch_batch_after_commit(batch_id=batch.id)
            )
    except IntegrityError:
        existing_batch = TransactionBatch.objects.filter(
            initiated_by=user,
            idempotency_key=effective_idempotency_key,
        ).first()
        if existing_batch is None:
            raise

        _validate_batch_request_fingerprint(
            batch=existing_batch,
            request_fingerprint=request_fingerprint,
        )
        _log_replayed_batch(existing_batch)
        return existing_batch, False

    log_event(
        logger,
        logging.INFO,
        "transaction.batch_accepted",
        message="Transaction batch accepted for asynchronous validation and processing.",
        user_id=user.id,
        status=batch.status,
        idempotency_key=batch.idempotency_key,
        count=batch.total_items,
    )
    publish_transaction_batch_status_update(batch_id=batch.id)
    return batch, True


def process_transaction_batch(*, batch_id: int) -> TransactionBatch:
    """Process transaction batch."""
    batch = TransactionBatch.objects.filter(id=batch_id).first()
    if batch is None:
        raise TransactionValidationError("Transaction batch does not exist.")

    if batch.status == TransactionBatch.Status.COMPLETED:
        return batch

    TransactionBatch.objects.filter(id=batch.id).update(
        status=TransactionBatch.Status.PROCESSING,
        processing_started_at=timezone.now(),
        failure_reason="",
    )
    batch.refresh_from_db()
    publish_transaction_batch_status_update(batch_id=batch.id)

    for item in batch.items.select_related("transaction").order_by("sequence", "id"):
        if item.transaction_id is not None or item.error_message:
            continue

        try:
            transaction, created = create_transfer(
                transfer_input=TransferInput(
                    user=batch.initiated_by,
                    from_account_iban=item.from_account_iban,
                    to_account_iban=item.to_account_iban,
                    amount=item.amount,
                    idempotency_key=item.idempotency_key,
                )
            )
        except (
            TransactionPermissionError,
            TransactionValidationError,
            IdempotencyConflictError,
        ) as exc:
            item.error_message = str(exc)
            item.save(update_fields=["error_message"])
            _update_batch_progress(batch_id=batch.id, succeeded=False)
            log_event(
                logger,
                logging.WARNING,
                "transaction.batch_item_failed",
                message="Transaction batch item failed validation.",
                transaction_id=None,
                user_id=batch.initiated_by_id,
                amount=item.amount,
                idempotency_key=item.idempotency_key,
                failure_reason=item.error_message,
            )
            continue

        item.transaction = transaction
        item.created_transaction = created
        item.error_message = ""
        item.save(
            update_fields=["transaction", "created_transaction", "error_message"]
        )
        _update_batch_progress(batch_id=batch.id, succeeded=True)

    _finalize_transaction_batch(batch_id=batch.id)
    batch.refresh_from_db()
    return batch


def _finalize_transaction_batch(*, batch_id: int) -> None:
    """Handle finalize transaction batch."""
    batch = TransactionBatch.objects.get(id=batch_id)
    items = list(batch.items.select_related("transaction"))
    processed_items = sum(
        1 for item in items if item.transaction_id is not None or item.error_message
    )
    succeeded_items = sum(1 for item in items if item.transaction_id is not None)
    failed_items = sum(1 for item in items if bool(item.error_message))

    batch.processed_items = processed_items
    batch.succeeded_items = succeeded_items
    batch.failed_items = failed_items
    batch.status = TransactionBatch.Status.COMPLETED
    batch.completed_at = timezone.now()
    batch.failure_reason = ""
    batch.save(
        update_fields=[
            "processed_items",
            "succeeded_items",
            "failed_items",
            "status",
            "completed_at",
            "failure_reason",
        ]
    )
    publish_transaction_batch_status_update(batch_id=batch.id)
    log_event(
        logger,
        logging.INFO,
        "transaction.batch_completed",
        message="Transaction batch processing completed.",
        user_id=batch.initiated_by_id,
        status=batch.status,
        idempotency_key=batch.idempotency_key,
        count=batch.total_items,
        )


def mark_transaction_batch_failed(*, batch_id: int, reason: str) -> None:
    """Handle mark transaction batch failed."""
    TransactionBatch.objects.filter(id=batch_id).update(
        status=TransactionBatch.Status.FAILED,
        completed_at=timezone.now(),
        failure_reason=reason,
    )
    batch = TransactionBatch.objects.filter(id=batch_id).first()
    if batch is not None:
        publish_transaction_batch_status_update(batch_id=batch.id)
        log_event(
            logger,
            logging.ERROR,
            "transaction.batch_failed",
            message="Transaction batch processing failed.",
            user_id=batch.initiated_by_id,
            status=batch.status,
            idempotency_key=batch.idempotency_key,
            count=batch.total_items,
            failure_reason=reason,
        )


def _update_batch_progress(*, batch_id: int, succeeded: bool) -> None:
    """Handle update batch progress."""
    batch = TransactionBatch.objects.get(id=batch_id)
    batch.processed_items += 1
    if succeeded:
        batch.succeeded_items += 1
    else:
        batch.failed_items += 1
    batch.save(
        update_fields=[
            "processed_items",
            "succeeded_items",
            "failed_items",
        ]
    )
    publish_transaction_batch_status_update(batch_id=batch.id)


def _dispatch_batch_after_commit(*, batch_id: int) -> None:
    """Handle dispatch batch after commit."""
    from apps.transactions.workers.celery_tasks import process_transaction_batch_task

    process_transaction_batch_task.delay(batch_id)


def _build_batch_fingerprint(*, items: list[BatchTransferItemInput]) -> str:
    """Handle build batch fingerprint."""
    serialized_items = [
        {
            key: str(value)
            for key, value in asdict(item).items()
        }
        for item in items
    ]
    raw_value = repr(serialized_items)
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _validate_batch_request_fingerprint(
    *,
    batch: TransactionBatch,
    request_fingerprint: str,
) -> None:
    """Handle validate batch request fingerprint."""
    if batch.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            "This Idempotency-Key is already used for a different batch payload."
        )


def _log_replayed_batch(batch: TransactionBatch) -> None:
    """Handle log replayed batch."""
    log_event(
        logger,
        logging.INFO,
        "transaction.batch_replayed",
        message="Existing transaction batch returned for the same idempotency key.",
        user_id=batch.initiated_by_id,
        status=batch.status,
        idempotency_key=batch.idempotency_key,
        count=batch.total_items,
    )
