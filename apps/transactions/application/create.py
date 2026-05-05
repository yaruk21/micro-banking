import logging
from django.db import IntegrityError

from apps.accounts.models import Account
from apps.transactions.models import Transaction

from .cache_versions import bump_pending_transaction_caches
from .exceptions import TransactionPermissionError, TransactionValidationError
from .idempotency import build_transfer_fingerprint, validate_request_fingerprint
from .types import TransferInput

logger = logging.getLogger("apps.transactions")


def create_transfer(*, transfer_input: TransferInput) -> tuple[Transaction, bool]:
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

    if not transfer_input.idempotency_key.strip():
        raise TransactionValidationError("Idempotency-Key header is required.")

    effective_idempotency_key = transfer_input.idempotency_key.strip()
    request_fingerprint = build_transfer_fingerprint(
        from_account_iban=from_account.iban,
        to_account_iban=to_account.iban,
        amount=transfer_input.amount,
    )

    existing_transfer = Transaction.objects.filter(
        initiated_by=transfer_input.user,
        idempotency_key=effective_idempotency_key,
    ).first()
    if existing_transfer is not None:
        validate_request_fingerprint(
            transfer=existing_transfer,
            request_fingerprint=request_fingerprint,
        )
        return existing_transfer, False

    try:
        transfer = Transaction.objects.create(
            initiated_by=transfer_input.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key=effective_idempotency_key,
            request_fingerprint=request_fingerprint,
            amount=transfer_input.amount,
            status=Transaction.Status.PENDING,
        )
    except IntegrityError:
        existing_transfer = Transaction.objects.filter(
            initiated_by=transfer_input.user,
            idempotency_key=effective_idempotency_key,
        ).first()
        if existing_transfer is None:
            raise

        validate_request_fingerprint(
            transfer=existing_transfer,
            request_fingerprint=request_fingerprint,
        )
        return existing_transfer, False

    logger.info(
        "Transaction queued id=%s from_account=%s to_account=%s amount=%s",
        transfer.id,
        transfer.from_account_id,
        transfer.to_account_id,
        transfer.amount,
    )
    bump_pending_transaction_caches(
        from_account=from_account,
        to_account=to_account,
    )
    return transfer, True
