import logging
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.accounts.models import Account
from core.cache_utils import bump_user_cache_version

from .models import Transaction

logger = logging.getLogger("apps.transactions")
User = get_user_model()


class TransactionError(Exception):
    pass


class TransactionPermissionError(TransactionError):
    pass


class TransactionValidationError(TransactionError):
    pass


@dataclass(frozen=True)
class TransferInput:
    user: User
    from_account_iban: str
    to_account_iban: str
    amount: Decimal


def create_transfer(*, transfer_input: TransferInput) -> Transaction:
    from_account = Account.objects.filter(iban=transfer_input.from_account_iban).first()
    to_account = Account.objects.filter(iban=transfer_input.to_account_iban).first()

    if from_account is None or to_account is None:
        raise TransactionValidationError("Both accounts must exist.")

    if from_account.owner_id != transfer_input.user.id:
        logger.warning(
            "Permission denied user=%s from_account_iban=%s",
            transfer_input.user.id,
            transfer_input.from_account_iban,
        )
        raise TransactionPermissionError(
            "You can transfer money only from your own accounts."
        )

    transfer = Transaction.objects.create(
        from_account=from_account,
        to_account=to_account,
        amount=transfer_input.amount,
        status=Transaction.Status.PENDING,
    )
    logger.info(
        "Transaction queued id=%s from_account=%s to_account=%s amount=%s",
        transfer.id,
        transfer.from_account_id,
        transfer.to_account_id,
        transfer.amount,
    )
    _bump_pending_transaction_caches(
        from_account=from_account,
        to_account=to_account,
    )
    return transfer


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

    logger.info(
        "Transaction completed id=%s from_account=%s to_account=%s amount=%s",
        transfer.id,
        transfer.from_account_id,
        transfer.to_account_id,
        transfer.amount,
    )
    _bump_transfer_related_caches(
        from_account=transfer.from_account,
        to_account=transfer.to_account,
    )
    return transfer


def _fail_transfer(*, transfer: Transaction, reason: str) -> Transaction:
    transfer.status = Transaction.Status.FAILED
    transfer.completed_at = timezone.now()
    transfer.failure_reason = reason
    transfer.save(update_fields=["status", "completed_at", "failure_reason"])
    _bump_failed_transaction_caches(
        from_account=transfer.from_account,
        to_account=transfer.to_account,
    )
    logger.warning(
        "Transaction failed id=%s from_account=%s to_account=%s amount=%s reason=%s",
        transfer.id,
        transfer.from_account_id,
        transfer.to_account_id,
        transfer.amount,
        reason,
    )
    return transfer


def _bump_pending_transaction_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)


def _bump_transfer_related_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="accounts_list", user_id=user_id)
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)


def _bump_failed_transaction_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)
