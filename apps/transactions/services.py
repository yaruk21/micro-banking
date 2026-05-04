import logging
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction

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

    if from_account.id == to_account.id:
        _create_failed_transaction(
            from_account=from_account,
            to_account=to_account,
            amount=transfer_input.amount,
            reason="Sender and recipient accounts must be different.",
        )
        _bump_failed_transaction_caches(
            from_account=from_account,
            to_account=to_account,
        )
        logger.warning(
            "Transaction failed from_account=%s to_account=%s amount=%s reason=%s",
            from_account.id,
            to_account.id,
            transfer_input.amount,
            "Sender and recipient accounts must be different.",
        )
        raise TransactionValidationError(
            "Sender and recipient accounts must be different."
        )

    try:
        with db_transaction.atomic():
            locked_accounts = {
                account.id: account
                for account in Account.objects.select_for_update()
                .filter(id__in=sorted({from_account.id, to_account.id}))
                .order_by("id")
            }
            if (
                from_account.id not in locked_accounts
                or to_account.id not in locked_accounts
            ):
                raise TransactionValidationError("Both accounts must exist.")

            locked_from_account = locked_accounts[from_account.id]
            locked_to_account = locked_accounts[to_account.id]

            if locked_from_account.currency != locked_to_account.currency:
                raise TransactionValidationError(
                    "Transfers are allowed only between accounts with the same currency."
                )

            if locked_from_account.balance < transfer_input.amount:
                raise TransactionValidationError("Insufficient balance.")

            locked_from_account.balance -= transfer_input.amount
            locked_to_account.balance += transfer_input.amount

            locked_from_account.save(update_fields=["balance"])
            locked_to_account.save(update_fields=["balance"])

            transfer = Transaction.objects.create(
                from_account=locked_from_account,
                to_account=locked_to_account,
                amount=transfer_input.amount,
                status=Transaction.Status.SUCCESS,
            )
    except TransactionValidationError as exc:
        failed_transaction = _create_failed_transaction(
            from_account=from_account,
            to_account=to_account,
            amount=transfer_input.amount,
            reason=str(exc),
        )
        _bump_failed_transaction_caches(
            from_account=from_account,
            to_account=to_account,
        )
        logger.warning(
            "Transaction failed from_account=%s to_account=%s amount=%s reason=%s",
            from_account.id,
            to_account.id,
            transfer_input.amount,
            exc,
        )
        raise

    logger.info(
        "Transaction success id=%s from_account=%s to_account=%s amount=%s",
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


def _create_failed_transaction(
    *,
    from_account: Account,
    to_account: Account,
    amount: Decimal,
    reason: str,
) -> Transaction:
    return Transaction.objects.create(
        from_account=from_account,
        to_account=to_account,
        amount=amount,
        status=Transaction.Status.FAILED,
        failure_reason=reason,
    )


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
