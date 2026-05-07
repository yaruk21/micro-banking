import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.accounts.cache import refresh_account_balance_cache
from apps.accounts.services import get_or_create_system_account
from apps.accounts.models import Account
from apps.transactions.models import Transaction
from apps.transactions.realtime import publish_transaction_status_update
from core.metrics import record_transaction_result
from core.structured_logging import log_event

from .cache_versions import (
    bump_failed_transaction_caches,
    bump_transfer_related_caches,
)
from .exchange import resolve_transfer_conversion
from .exceptions import TransactionValidationError

logger = logging.getLogger("apps.transactions")


def process_transfer(*, transaction_id: int) -> Transaction:
    """Process transfer."""
    with db_transaction.atomic():
        transfer = (
            Transaction.objects.select_for_update()
            .filter(id=transaction_id)
            .first()
        )
        if transfer is None:
            raise TransactionValidationError("Transaction does not exist.")

        if transfer.status == Transaction.Status.COMPLETED:
            return transfer

        if transfer.status == Transaction.Status.FAILED:
            return transfer

        transfer.status = Transaction.Status.PROCESSING
        transfer.processing_started_at = timezone.now()
        transfer.failure_reason = ""
        transfer.save(
            update_fields=["status", "processing_started_at", "failure_reason"]
        )
        log_event(
            logger,
            logging.INFO,
            "transaction.processing_started",
            message="Transaction processing started.",
            transaction_id=transfer.id,
            user_id=transfer.initiated_by_id,
            from_account_id=transfer.from_account_id,
            to_account_id=transfer.to_account_id,
            amount=transfer.amount,
            status=transfer.status,
            idempotency_key=transfer.idempotency_key,
        )
        publish_transaction_status_update(transaction_id=transfer.id)

        locked_accounts = {
            account.id: account
            for account in Account.objects.select_for_update()
            .filter(id__in=sorted({transfer.from_account_id, transfer.to_account_id}))
            .order_by("id")
        }
        if (
            transfer.from_account_id not in locked_accounts
            or transfer.to_account_id not in locked_accounts
        ):
            return _fail_transfer(
                transfer=transfer,
                reason="Both accounts must exist.",
            )

        locked_from_account = locked_accounts[transfer.from_account_id]
        locked_to_account = locked_accounts[transfer.to_account_id]
        fee_account = None

        if locked_from_account.id == locked_to_account.id:
            return _fail_transfer(
                transfer=transfer,
                reason="Sender and recipient accounts must be different.",
            )

        if transfer.exchange_rate is None or transfer.credited_amount is None:
            (
                transfer.exchange_rate,
                transfer.credited_amount,
                transfer.fee_amount,
                transfer.fee_currency,
                transfer.exchange_rate_provider,
            ) = resolve_transfer_conversion(
                from_account=locked_from_account,
                to_account=locked_to_account,
                amount=transfer.amount,
            )
            transfer.save(
                update_fields=[
                    "exchange_rate",
                    "credited_amount",
                    "fee_amount",
                    "fee_currency",
                    "exchange_rate_provider",
                ]
            )
        if (transfer.fee_amount or Decimal("0.00")) > Decimal("0.00"):
            fee_account = get_or_create_system_account(currency=locked_to_account.currency)
            fee_account = (
                Account.objects.select_for_update()
                .filter(id=fee_account.id)
                .first()
            )
            if fee_account is None:
                return _fail_transfer(
                    transfer=transfer,
                    reason="System fee account is not available.",
                )

        if locked_from_account.balance < transfer.amount:
            return _fail_transfer(
                transfer=transfer,
                reason="Insufficient balance.",
            )

        locked_from_account.balance -= transfer.amount
        locked_to_account.balance += transfer.credited_amount
        if fee_account is not None:
            fee_account.balance += transfer.fee_amount

        locked_from_account.save(update_fields=["balance"])
        locked_to_account.save(update_fields=["balance"])
        if fee_account is not None:
            fee_account.save(update_fields=["balance"])

        transfer.status = Transaction.Status.COMPLETED
        transfer.completed_at = timezone.now()
        transfer.failure_reason = ""
        transfer.save(update_fields=["status", "completed_at", "failure_reason"])

    log_event(
        logger,
        logging.INFO,
        "transaction.completed",
        message="Transaction completed successfully.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        to_account_id=transfer.to_account_id,
        amount=transfer.amount,
        status=transfer.status,
        idempotency_key=transfer.idempotency_key,
    )
    bump_transfer_related_caches(
        from_account=locked_from_account,
        to_account=locked_to_account,
    )
    if fee_account is not None:
        refresh_account_balance_cache(account=fee_account)
    publish_transaction_status_update(transaction_id=transfer.id)
    record_transaction_result(
        transfer_type=transfer.transfer_type,
        status=transfer.status,
        amount=transfer.amount,
        duration_seconds=_processing_duration_seconds(transfer=transfer),
    )
    return transfer


def _fail_transfer(*, transfer: Transaction, reason: str) -> Transaction:
    """Handle fail transfer."""
    transfer.status = Transaction.Status.FAILED
    transfer.completed_at = timezone.now()
    transfer.failure_reason = reason
    transfer.save(update_fields=["status", "completed_at", "failure_reason"])
    bump_failed_transaction_caches(
        from_account=transfer.from_account,
        to_account=transfer.to_account,
    )
    log_event(
        logger,
        logging.WARNING,
        "transaction.failed",
        message="Transaction processing failed.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        to_account_id=transfer.to_account_id,
        amount=transfer.amount,
        status=transfer.status,
        idempotency_key=transfer.idempotency_key,
        failure_reason=reason,
    )
    publish_transaction_status_update(transaction_id=transfer.id)
    record_transaction_result(
        transfer_type=transfer.transfer_type,
        status=transfer.status,
        amount=transfer.amount,
        duration_seconds=_processing_duration_seconds(transfer=transfer),
    )
    return transfer


def _processing_duration_seconds(*, transfer: Transaction) -> float:
    """Return processing duration from the recorded processing start time."""

    if transfer.processing_started_at is None or transfer.completed_at is None:
        return 0.0
    return max(
        (transfer.completed_at - transfer.processing_started_at).total_seconds(),
        0.0,
    )
