import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.transactions.models import (
    SwiftTransferDetails,
    Transaction,
    TransactionIdempotencyKey,
)
from apps.transactions.realtime import publish_transaction_status_update
from core.cache_utils import bump_user_cache_version
from core.structured_logging import log_event

from .cache_versions import bump_pending_transaction_caches
from .challenge import (
    create_transaction_challenge,
    should_create_transaction_challenge,
)
from .exceptions import (
    IdempotencyConflictError,
    TransactionPermissionError,
    TransactionValidationError,
)
from .exchange import resolve_transfer_conversion
from .fraud import (
    attach_fraud_event_transaction,
    create_transaction_attempt_event,
    raise_for_fraud_decision,
)
from .idempotency import (
    build_swift_transfer_fingerprint,
    build_transfer_fingerprint,
    validate_request_fingerprint,
)
from .limits import enforce_transaction_limits
from .outbox import (
    create_transaction_outbox,
    register_transaction_outbox_publish,
)
from .types import SwiftTransferInput, TransferInput

logger = logging.getLogger("apps.transactions")
SWIFT_FEE_FIXED = Decimal("10.00")
SWIFT_FEE_RATE = Decimal("0.0100")


# Accepts a client transfer request and preserves async/idempotent semantics.
def create_transfer(*, transfer_input: TransferInput) -> tuple[Transaction, bool]:
    """Create or replay a transfer using the external idempotency registry."""

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

    existing_registry = TransactionIdempotencyKey.objects.filter(
        initiated_by=transfer_input.user,
        idempotency_key=effective_idempotency_key,
    ).first()
    if existing_registry is not None:
        existing_transfer = Transaction.objects.filter(
            id=existing_registry.transaction_id,
        ).first()
        if existing_transfer is None:
            raise TransactionValidationError(
                "Existing transaction could not be resolved for the idempotency key."
            )
        try:
            validate_request_fingerprint(
                existing_request_fingerprint=existing_registry.request_fingerprint,
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

    fraud_event, fraud_decision = create_transaction_attempt_event(
        user=transfer_input.user,
        amount=transfer_input.amount,
        fraud_context=transfer_input.fraud_context,
    )
    raise_for_fraud_decision(decision=fraud_decision)

    enforce_transaction_limits(
        user=transfer_input.user,
        amount=transfer_input.amount,
    )

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
                transfer_type=Transaction.TransferType.INTERNAL,
            )
            TransactionIdempotencyKey.objects.create(
                initiated_by=transfer_input.user,
                idempotency_key=effective_idempotency_key,
                request_fingerprint=request_fingerprint,
                transaction_id=transfer.id,
            )
            challenge_reason_codes = should_create_transaction_challenge(
                amount=transfer_input.amount,
                fraud_decision=fraud_decision,
            )
            if challenge_reason_codes:
                create_transaction_challenge(
                    transaction=transfer,
                    user=transfer_input.user,
                    reason_codes=challenge_reason_codes,
                )
            else:
                outbox = create_transaction_outbox(transaction=transfer)
                register_transaction_outbox_publish(outbox_id=outbox.id)
            attach_fraud_event_transaction(
                fraud_event=fraud_event,
                transaction=transfer,
            )
    except IntegrityError:
        existing_registry = TransactionIdempotencyKey.objects.filter(
            initiated_by=transfer_input.user,
            idempotency_key=effective_idempotency_key,
        ).first()
        if existing_registry is None:
            raise

        existing_transfer = Transaction.objects.filter(
            id=existing_registry.transaction_id,
        ).first()
        if existing_transfer is None:
            raise TransactionValidationError(
                "Existing transaction could not be resolved for the idempotency key."
            )

        try:
            validate_request_fingerprint(
                existing_request_fingerprint=existing_registry.request_fingerprint,
                request_fingerprint=request_fingerprint,
            )
        except IdempotencyConflictError:
            _log_idempotency_conflict(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
            raise
        attach_fraud_event_transaction(
            fraud_event=fraud_event,
            transaction=existing_transfer,
        )
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


def create_swift_transfer(
    *,
    transfer_input: SwiftTransferInput,
) -> tuple[Transaction, bool]:
    """Create or replay a SWIFT transfer without dispatching the internal worker."""

    from_account = Account.objects.filter(iban=transfer_input.from_account_iban).first()
    if from_account is None:
        raise TransactionValidationError("Sender account must exist.")

    if from_account.owner_id != transfer_input.user.id:
        log_event(
            logger,
            logging.WARNING,
            "transaction.permission_denied",
            message="Permission denied for SWIFT transfer creation.",
            user_id=transfer_input.user.id,
            idempotency_key=transfer_input.idempotency_key.strip() or None,
        )
        raise TransactionPermissionError(
            "You can transfer money only from your own accounts."
        )

    if not transfer_input.idempotency_key.strip():
        raise TransactionValidationError("Idempotency-Key header is required.")

    effective_idempotency_key = transfer_input.idempotency_key.strip()
    request_fingerprint = build_swift_transfer_fingerprint(
        from_account_iban=from_account.iban,
        amount=transfer_input.amount,
        swift_code=transfer_input.swift_code,
        beneficiary_name=transfer_input.beneficiary_name,
        beneficiary_account_number=transfer_input.beneficiary_account_number,
        beneficiary_iban=transfer_input.beneficiary_iban,
        beneficiary_bank_name=transfer_input.beneficiary_bank_name,
        beneficiary_bank_country=transfer_input.beneficiary_bank_country,
        beneficiary_address=transfer_input.beneficiary_address,
        swift_reference=transfer_input.swift_reference,
    )

    existing_registry = TransactionIdempotencyKey.objects.filter(
        initiated_by=transfer_input.user,
        idempotency_key=effective_idempotency_key,
    ).first()
    if existing_registry is not None:
        existing_transfer = Transaction.objects.filter(
            id=existing_registry.transaction_id,
        ).first()
        if existing_transfer is None:
            raise TransactionValidationError(
                "Existing transaction could not be resolved for the idempotency key."
            )
        try:
            validate_request_fingerprint(
                existing_request_fingerprint=existing_registry.request_fingerprint,
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

    fraud_event, fraud_decision = create_transaction_attempt_event(
        user=transfer_input.user,
        amount=transfer_input.amount,
        fraud_context=transfer_input.fraud_context,
    )
    raise_for_fraud_decision(decision=fraud_decision)

    enforce_transaction_limits(
        user=transfer_input.user,
        amount=transfer_input.amount,
    )

    created_at = timezone.now()
    scheduled_processing_at = _add_business_days(created_at, 1)
    expected_completion_at = _add_business_days(
        created_at,
        _get_swift_completion_business_days(
            request_fingerprint=request_fingerprint,
        ),
    )
    if expected_completion_at < scheduled_processing_at:
        expected_completion_at = scheduled_processing_at
    swift_fee_amount = (
        SWIFT_FEE_FIXED + (transfer_input.amount * SWIFT_FEE_RATE)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    try:
        with db_transaction.atomic():
            transfer = Transaction.objects.create(
                initiated_by=transfer_input.user,
                from_account=from_account,
                to_account=None,
                idempotency_key=effective_idempotency_key,
                request_fingerprint=request_fingerprint,
                amount=transfer_input.amount,
                credited_amount=transfer_input.amount,
                exchange_rate=Decimal("1.00000000"),
                exchange_rate_provider="swift",
                fee_amount=swift_fee_amount,
                fee_currency=from_account.currency,
                status=Transaction.Status.PENDING,
                transfer_type=Transaction.TransferType.SWIFT,
            )
            SwiftTransferDetails.objects.create(
                transaction=transfer,
                swift_code=transfer_input.swift_code,
                beneficiary_name=transfer_input.beneficiary_name,
                beneficiary_account_number=transfer_input.beneficiary_account_number,
                beneficiary_iban=transfer_input.beneficiary_iban,
                beneficiary_bank_name=transfer_input.beneficiary_bank_name,
                beneficiary_bank_country=transfer_input.beneficiary_bank_country,
                beneficiary_address=transfer_input.beneficiary_address,
                swift_reference=transfer_input.swift_reference,
                scheduled_processing_at=scheduled_processing_at,
                expected_completion_at=expected_completion_at,
                swift_fee_fixed=SWIFT_FEE_FIXED,
                swift_fee_rate=SWIFT_FEE_RATE,
            )
            TransactionIdempotencyKey.objects.create(
                initiated_by=transfer_input.user,
                idempotency_key=effective_idempotency_key,
                request_fingerprint=request_fingerprint,
                transaction_id=transfer.id,
            )
            challenge_reason_codes = should_create_transaction_challenge(
                amount=transfer_input.amount,
                fraud_decision=fraud_decision,
            )
            if challenge_reason_codes:
                create_transaction_challenge(
                    transaction=transfer,
                    user=transfer_input.user,
                    reason_codes=challenge_reason_codes,
                )
            attach_fraud_event_transaction(
                fraud_event=fraud_event,
                transaction=transfer,
            )
    except IntegrityError:
        existing_registry = TransactionIdempotencyKey.objects.filter(
            initiated_by=transfer_input.user,
            idempotency_key=effective_idempotency_key,
        ).first()
        if existing_registry is None:
            raise

        existing_transfer = Transaction.objects.filter(
            id=existing_registry.transaction_id,
        ).first()
        if existing_transfer is None:
            raise TransactionValidationError(
                "Existing transaction could not be resolved for the idempotency key."
            )

        try:
            validate_request_fingerprint(
                existing_request_fingerprint=existing_registry.request_fingerprint,
                request_fingerprint=request_fingerprint,
            )
        except IdempotencyConflictError:
            _log_idempotency_conflict(
                transfer=existing_transfer,
                request_fingerprint=request_fingerprint,
            )
            raise
        attach_fraud_event_transaction(
            fraud_event=fraud_event,
            transaction=existing_transfer,
        )
        _log_replayed_transaction(existing_transfer)
        return existing_transfer, False

    log_event(
        logger,
        logging.INFO,
        "transaction.swift_accepted",
        message="SWIFT transaction accepted for deferred processing.",
        transaction_id=transfer.id,
        user_id=transfer.initiated_by_id,
        from_account_id=transfer.from_account_id,
        amount=transfer.amount,
        status=transfer.status,
        transfer_type=transfer.transfer_type,
        idempotency_key=transfer.idempotency_key,
        request_fingerprint=transfer.request_fingerprint,
        scheduled_processing_at=scheduled_processing_at,
        expected_completion_at=expected_completion_at,
    )
    bump_user_cache_version(
        namespace="transactions_list",
        user_id=from_account.owner_id,
    )
    publish_transaction_status_update(transaction_id=transfer.id)
    return transfer, True


# Emits a structured log when the same idempotency key is reused with a new payload.
def _log_idempotency_conflict(
    *,
    transfer: Transaction,
    request_fingerprint: str,
) -> None:
    """Write a structured conflict log for idempotency mismatches."""

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


# Emits a structured log when an existing transaction is returned for a replay.
def _log_replayed_transaction(transfer: Transaction) -> None:
    """Write a structured replay log for successful idempotent retries."""

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


def _add_business_days(start, business_days: int):
    """Return a datetime shifted by the requested number of business days."""

    current = start
    added = 0
    while added < business_days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _get_swift_completion_business_days(*, request_fingerprint: str) -> int:
    """Return a deterministic 1-3 business day completion window for SWIFT."""

    try:
        fingerprint_sample = int(request_fingerprint[:2], 16)
    except ValueError:
        return 3

    return 1 + (fingerprint_sample % 3)
