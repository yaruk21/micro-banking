import logging

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.transactions.models import Transaction
from core.structured_logging import log_event

from .cache_versions import (
    bump_failed_transaction_caches,
    bump_transfer_related_caches,
)
from .exceptions import TransactionValidationError

logger = logging.getLogger("apps.transactions")


def process_transfer(*, transaction_id: int) -> Transaction:
    with db_transaction.atomic():
        transfer = (
            Transaction.objects.select_for_update()
            .select_related("from_account", "to_account")
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

        if locked_from_account.id == locked_to_account.id:
            return _fail_transfer(
                transfer=transfer,
                reason="Sender and recipient accounts must be different.",
            )

        if locked_from_account.currency != locked_to_account.currency:
            return _fail_transfer(
                transfer=transfer,
                reason="Transfers are allowed only between accounts with the same currency.",
            )

        if locked_from_account.balance < transfer.amount:
            return _fail_transfer(
                transfer=transfer,
                reason="Insufficient balance.",
            )

        locked_from_account.balance -= transfer.amount
        locked_to_account.balance += transfer.amount

        locked_from_account.save(update_fields=["balance"])
        locked_to_account.save(update_fields=["balance"])

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
        from_account=transfer.from_account,
        to_account=transfer.to_account,
    )
    return transfer


def _fail_transfer(*, transfer: Transaction, reason: str) -> Transaction:
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
    return transfer
