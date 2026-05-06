import logging
from decimal import Decimal
from django.db import IntegrityError, transaction as db_transaction

from apps.accounts.models import Account
from apps.transactions.models import Transaction
from apps.transactions.realtime import publish_transaction_status_update
from core.structured_logging import log_event

from .cache_versions import bump_pending_transaction_caches
from .exceptions import (
    IdempotencyConflictError,
    TransactionPermissionError,
    TransactionValidationError,
)
from .exchange import resolve_transfer_conversion
from .idempotency import build_transfer_fingerprint, validate_request_fingerprint
from .outbox import (
    create_transaction_outbox,
    register_transaction_outbox_publish,
)
from .types import TransferInput

logger = logging.getLogger("apps.transactions")


def create_transfer(*, transfer_input: TransferInput) -> tuple[Transaction, bool]:
    from_account = Account.objects.filter(iban=transfer_input.from_account_iban).first()
    to_account = Account.objects.filter(iban=transfer_input.to_account_iban).first()

    if from_account is None or to_account is None:
        raise TransactionValidationError("Both accounts must exist.")

    if from_account.owner_id != transfer_input.user.id:
        log_event(
            logger,
            logging.WARNING,
            "transaction.permission_denied",
            message="Permission denied for transfer creation.",
            user_id=transfer_input.user.id,
            idempotency_key=transfer_input.idempotency_key.strip() or None,
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
        try:
            validate_request_fingerprint(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
        except IdempotencyConflictError:
            _log_idempotency_conflict(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
            raise
        _log_replayed_transaction(existing_transfer)
        return existing_transfer, False

    (
        exchange_rate,
        credited_amount,
        fee_amount,
        fee_currency,
        exchange_rate_provider,
    ) = (
        resolve_transfer_conversion(
            from_account=from_account,
            to_account=to_account,
            amount=transfer_input.amount,
        )
    )

    try:
        with db_transaction.atomic():
            transfer = Transaction.objects.create(
                initiated_by=transfer_input.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=effective_idempotency_key,
                request_fingerprint=request_fingerprint,
                amount=transfer_input.amount,
                credited_amount=credited_amount,
                exchange_rate=exchange_rate,
                exchange_rate_provider=exchange_rate_provider,
                fee_amount=fee_amount,
                fee_currency=fee_currency,
                status=Transaction.Status.PENDING,
            )
            outbox = create_transaction_outbox(transaction=transfer)
            register_transaction_outbox_publish(outbox_id=outbox.id)
    except IntegrityError:
        existing_transfer = Transaction.objects.filter(
            initiated_by=transfer_input.user,
            idempotency_key=effective_idempotency_key,
        ).first()
        if existing_transfer is None:
            raise

        try:
            validate_request_fingerprint(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
        except IdempotencyConflictError:
            _log_idempotency_conflict(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
            raise
        _log_replayed_transaction(existing_transfer)
        return existing_transfer, False

    log_event(
        logger,
        logging.INFO,
        "transaction.accepted",
        message="Transaction accepted for asynchronous processing.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        to_account_id=transfer.to_account_id,
        amount=transfer.amount,
        status=transfer.status,
        idempotency_key=transfer.idempotency_key,
        request_fingerprint=transfer.request_fingerprint,
    )
    bump_pending_transaction_caches(
        from_account=from_account,
        to_account=to_account,
    )
    publish_transaction_status_update(transaction_id=transfer.id)
    return transfer, True


def _log_idempotency_conflict(
    *,
    transfer: Transaction,
    request_fingerprint: str,
) -> None:
    log_event(
        logger,
        logging.WARNING,
        "transaction.idempotency_conflict",
        message="Idempotency key reused with a different payload.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        to_account_id=transfer.to_account_id,
        amount=transfer.amount,
        status=transfer.status,
        idempotency_key=transfer.idempotency_key,
        request_fingerprint=request_fingerprint,
    )


def _log_replayed_transaction(transfer: Transaction) -> None:
    log_event(
        logger,
        logging.INFO,
        "transaction.replayed",
        message="Existing transaction returned for the same idempotency key.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        to_account_id=transfer.to_account_id,
        amount=transfer.amount,
        status=transfer.status,
        idempotency_key=transfer.idempotency_key,
        request_fingerprint=transfer.request_fingerprint,
    )
