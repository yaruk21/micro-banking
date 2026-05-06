import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.cache import refresh_account_balance_cache
from apps.accounts.models import Account
from apps.accounts.services import get_or_create_system_account
from apps.transactions.models import SwiftTransferDetails, Transaction
from apps.transactions.realtime import publish_transaction_status_update
from core.cache_utils import bump_user_cache_version
from core.structured_logging import log_event

from .exceptions import TransactionValidationError

logger = logging.getLogger("apps.transactions")
ZERO_AMOUNT = Decimal("0.00")


def get_due_swift_transaction_ids(
    *,
    limit: int = 100,
    now=None,
) -> list[int]:
    """Return due SWIFT ids for either processing start or final completion."""

    reference_time = now or timezone.now()
    return list(
        Transaction.objects.filter(
            transfer_type=Transaction.TransferType.SWIFT,
        ).filter(
            Q(
                status=Transaction.Status.PENDING,
                swift_details__scheduled_processing_at__isnull=False,
                swift_details__scheduled_processing_at__lte=reference_time,
            )
            | Q(
                status=Transaction.Status.PROCESSING,
                swift_details__expected_completion_at__isnull=False,
                swift_details__expected_completion_at__lte=reference_time,
            )
        )
        .order_by(
            "swift_details__expected_completion_at",
            "swift_details__scheduled_processing_at",
            "id",
        )
        .values_list("id", flat=True)[:limit]
    )


def process_swift_transfer(*, transaction_id: int) -> Transaction:
    """Process a due SWIFT transfer and post its fee."""

    current_time = timezone.now()

    with db_transaction.atomic():
        transfer = (
            Transaction.objects.select_for_update()
            .select_related("from_account", "swift_details")
            .filter(id=transaction_id)
            .first()
        )
        if transfer is None:
            raise TransactionValidationError("Transaction does not exist.")

        if transfer.transfer_type != Transaction.TransferType.SWIFT:
            raise TransactionValidationError("Transaction is not a SWIFT transfer.")

        if transfer.status == Transaction.Status.COMPLETED:
            return transfer

        if transfer.status == Transaction.Status.FAILED:
            return transfer

        try:
            swift_details = transfer.swift_details
        except SwiftTransferDetails.DoesNotExist as exc:
            raise TransactionValidationError(
                "SWIFT transfer details are missing."
            ) from exc

        if (
            swift_details.scheduled_processing_at is not None
            and swift_details.scheduled_processing_at > current_time
        ):
            return transfer

        transfer.status = Transaction.Status.PROCESSING
        transfer.processing_started_at = current_time
        transfer.failure_reason = ""
        transfer.save(
            update_fields=["status", "processing_started_at", "failure_reason"]
        )
        log_event(
            logger,
            logging.INFO,
            "transaction.swift_processing_started",
            message="SWIFT transaction processing started.",
            transaction_id=transfer.id,
            user_id=transfer.initiated_by_id,
            from_account_id=transfer.from_account_id,
            amount=transfer.amount,
            status=transfer.status,
            transfer_type=transfer.transfer_type,
            idempotency_key=transfer.idempotency_key,
        )
        publish_transaction_status_update(transaction_id=transfer.id)
        bump_user_cache_version(
            namespace="transactions_list",
            user_id=transfer.initiated_by_id,
        )

        if (
            swift_details.expected_completion_at is None
            or swift_details.expected_completion_at > current_time
        ):
            return transfer

        locked_from_account = (
            Account.objects.select_for_update()
            .filter(id=transfer.from_account_id)
            .first()
        )
        if locked_from_account is None:
            return _fail_swift_transfer(
                transfer=transfer,
                reason="Sender account must exist.",
            )

        total_debit = transfer.amount + (transfer.fee_amount or ZERO_AMOUNT)
        if locked_from_account.balance < total_debit:
            return _fail_swift_transfer(
                transfer=transfer,
                reason="Insufficient balance to cover SWIFT amount and fee.",
            )

        fee_account = None
        if (transfer.fee_amount or ZERO_AMOUNT) > ZERO_AMOUNT:
            fee_account = get_or_create_system_account(currency=locked_from_account.currency)
            fee_account = (
                Account.objects.select_for_update()
                .filter(id=fee_account.id)
                .first()
            )
            if fee_account is None:
                return _fail_swift_transfer(
                    transfer=transfer,
                    reason="System fee account is not available.",
                )

        locked_from_account.balance -= total_debit
        locked_from_account.save(update_fields=["balance"])

        if fee_account is not None:
            fee_account.balance += transfer.fee_amount or ZERO_AMOUNT
            fee_account.save(update_fields=["balance"])

        transfer.status = Transaction.Status.COMPLETED
        transfer.completed_at = timezone.now()
        transfer.failure_reason = ""
        transfer.save(update_fields=["status", "completed_at", "failure_reason"])

    log_event(
        logger,
        logging.INFO,
        "transaction.swift_completed",
        message="SWIFT transaction completed successfully.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        amount=transfer.amount,
        status=transfer.status,
        transfer_type=transfer.transfer_type,
        idempotency_key=transfer.idempotency_key,
    )
    refresh_account_balance_cache(account=locked_from_account)
    if fee_account is not None:
        refresh_account_balance_cache(account=fee_account)
    bump_user_cache_version(namespace="accounts_list", user_id=locked_from_account.owner_id)
    bump_user_cache_version(
        namespace="transactions_list",
        user_id=locked_from_account.owner_id,
    )
    publish_transaction_status_update(transaction_id=transfer.id)
    return transfer


def _fail_swift_transfer(*, transfer: Transaction, reason: str) -> Transaction:
    """Mark a SWIFT transfer as failed and publish status updates."""

    transfer.status = Transaction.Status.FAILED
    transfer.completed_at = timezone.now()
    transfer.failure_reason = reason
    transfer.save(update_fields=["status", "completed_at", "failure_reason"])
    log_event(
        logger,
        logging.WARNING,
        "transaction.swift_failed",
        message="SWIFT transaction processing failed.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        amount=transfer.amount,
        status=transfer.status,
        transfer_type=transfer.transfer_type,
        idempotency_key=transfer.idempotency_key,
        failure_reason=reason,
    )
    bump_user_cache_version(
        namespace="transactions_list",
        user_id=transfer.initiated_by_id,
    )
    publish_transaction_status_update(transaction_id=transfer.id)
    return transfer
