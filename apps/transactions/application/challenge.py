import logging
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.transactions.models import FraudEvent, Transaction, TransactionChallenge
from core.structured_logging import log_event

from .exceptions import TransactionPermissionError, TransactionValidationError
from .outbox import create_transaction_outbox, register_transaction_outbox_publish

logger = logging.getLogger("apps.transactions")

CHALLENGE_REASON_FRAUD_FLAGGED = "fraud_flagged"
CHALLENGE_REASON_FRAUD_CHALLENGE = "fraud_challenge"
CHALLENGE_REASON_LARGE_AMOUNT = "large_amount"


def should_create_transaction_challenge(*, amount: Decimal, fraud_decision) -> list[str]:
    """Return challenge reason codes for transactions that need 2FA confirmation."""

    reason_codes: list[str] = []
    if fraud_decision.action == "challenge":
        reason_codes.append(CHALLENGE_REASON_FRAUD_CHALLENGE)
    elif (
        fraud_decision.action == "flag"
        and settings.TRANSACTION_2FA_CHALLENGE_FLAGGED
    ):
        reason_codes.append(CHALLENGE_REASON_FRAUD_FLAGGED)

    large_amount_threshold = settings.TRANSACTION_2FA_CHALLENGE_AMOUNT
    if large_amount_threshold > Decimal("0.00") and amount >= large_amount_threshold:
        reason_codes.append(CHALLENGE_REASON_LARGE_AMOUNT)

    return reason_codes


def create_transaction_challenge(
    *,
    transaction: Transaction,
    user,
    reason_codes: list[str],
) -> TransactionChallenge:
    """Create and persist one pending transaction challenge."""

    challenge_code = _generate_challenge_code()
    challenge = TransactionChallenge.objects.create(
        user=user,
        transaction=transaction,
        status=TransactionChallenge.Status.PENDING,
        code_hash=make_password(challenge_code),
        reason_codes=",".join(reason_codes),
        max_attempts=settings.TRANSACTION_2FA_CHALLENGE_MAX_ATTEMPTS,
        expires_at=timezone.now()
        + timedelta(seconds=settings.TRANSACTION_2FA_CHALLENGE_TTL_SECONDS),
    )
    transaction._challenge_code = challenge_code

    FraudEvent.objects.create(
        user=user,
        transaction=transaction,
        event_type=FraudEvent.EventType.CHALLENGE,
        outcome=FraudEvent.Outcome.FLAGGED,
    )
    log_event(
        logger,
        logging.INFO,
        "transaction.challenge_created",
        message="Transaction challenge created and awaits 2FA confirmation.",
        transaction_id=transaction.id,
        user_id=user.id,
        status=transaction.status,
        challenge_id=challenge.id,
        challenge_reason_codes=reason_codes,
        challenge_expires_at=challenge.expires_at,
    )
    return challenge


def confirm_transaction_challenge(
    *,
    user,
    transaction_id: int,
    code: str,
) -> Transaction:
    """Verify a 2FA code and resume the accepted transaction flow."""

    normalized_code = code.strip()
    if not normalized_code:
        raise TransactionValidationError("2FA code is required.")

    validation_error_message: Optional[str] = None
    should_publish_status = False

    with db_transaction.atomic():
        transaction = (
            Transaction.objects.select_for_update()
            .filter(id=transaction_id)
            .first()
        )
        if transaction is None:
            raise TransactionValidationError("Transaction does not exist.")

        if transaction.initiated_by_id != user.id:
            raise TransactionPermissionError(
                "You can confirm 2FA only for your own transactions."
            )

        challenge = (
            TransactionChallenge.objects.select_for_update()
            .filter(transaction_id=transaction.id)
            .first()
        )
        if challenge is None:
            raise TransactionValidationError(
                "Transaction does not require 2FA confirmation."
            )

        if challenge.status == TransactionChallenge.Status.VERIFIED:
            return transaction

        _expire_transaction_challenge_if_due(
            transaction=transaction,
            challenge=challenge,
        )
        challenge.refresh_from_db()
        transaction.refresh_from_db()

        if challenge.status == TransactionChallenge.Status.EXPIRED:
            validation_error_message = "2FA challenge has expired."
            should_publish_status = True
        elif challenge.status == TransactionChallenge.Status.FAILED:
            validation_error_message = "2FA challenge is no longer valid."
            should_publish_status = True
        else:
            challenge.attempts_count += 1
            if not check_password(normalized_code, challenge.code_hash):
                if challenge.attempts_count >= challenge.max_attempts:
                    _fail_transaction_challenge(
                        transaction=transaction,
                        challenge=challenge,
                        failure_reason=(
                            "2FA challenge failed after too many invalid attempts."
                        ),
                    )
                    validation_error_message = (
                        "2FA challenge failed after too many invalid attempts."
                    )
                else:
                    challenge.save(update_fields=["attempts_count", "updated_at"])
                    validation_error_message = "Invalid 2FA code."
                should_publish_status = True
            else:
                challenge.status = TransactionChallenge.Status.VERIFIED
                challenge.verified_at = timezone.now()
                challenge.save(
                    update_fields=[
                        "status",
                        "verified_at",
                        "attempts_count",
                        "updated_at",
                    ]
                )
                FraudEvent.objects.create(
                    user=user,
                    transaction=transaction,
                    event_type=FraudEvent.EventType.CHALLENGE,
                    outcome=FraudEvent.Outcome.ALLOWED,
                )
                _resume_transaction_after_verified_challenge(transaction=transaction)
                should_publish_status = True
                log_event(
                    logger,
                    logging.INFO,
                    "transaction.challenge_verified",
                    message="Transaction challenge verified successfully.",
                    transaction_id=transaction.id,
                    user_id=user.id,
                    status=transaction.status,
                    transfer_type=transaction.transfer_type,
                )

    if should_publish_status:
        from apps.transactions.realtime import publish_transaction_status_update

        publish_transaction_status_update(transaction_id=transaction.id)
    if validation_error_message is not None:
        raise TransactionValidationError(validation_error_message)
    return transaction


def sync_transaction_challenge_state(*, transaction: Transaction) -> Transaction:
    """Expire one pending challenge lazily when the transaction is observed later."""

    try:
        current_challenge = transaction.challenge
    except TransactionChallenge.DoesNotExist:
        return transaction

    if current_challenge.status != TransactionChallenge.Status.PENDING:
        return transaction

    with db_transaction.atomic():
        locked_transaction = (
            Transaction.objects.select_for_update()
            .filter(id=transaction.id)
            .first()
        )
        if locked_transaction is None:
            return transaction

        locked_challenge = (
            TransactionChallenge.objects.select_for_update()
            .filter(transaction_id=locked_transaction.id)
            .first()
        )
        if locked_challenge is None:
            return locked_transaction

        previous_status = locked_challenge.status
        _expire_transaction_challenge_if_due(
            transaction=locked_transaction,
            challenge=locked_challenge,
        )

    if previous_status != locked_challenge.status:
        from apps.transactions.realtime import publish_transaction_status_update

        publish_transaction_status_update(transaction_id=locked_transaction.id)
    return locked_transaction


def get_debug_transaction_challenge_code(*, transaction: Transaction) -> Optional[str]:
    """Return a transient challenge code attached during the current request."""

    return getattr(transaction, "_challenge_code", None)


def transaction_requires_2fa(*, transaction: Transaction) -> bool:
    """Return whether the transaction currently waits for 2FA confirmation."""

    challenge = get_transaction_challenge(transaction=transaction)
    if challenge is None:
        return False
    return challenge.status == TransactionChallenge.Status.PENDING


def get_transaction_challenge_reason_codes(*, challenge: TransactionChallenge) -> list[str]:
    """Return normalized reason codes for one persisted challenge."""

    if not challenge.reason_codes:
        return []
    return [code for code in challenge.reason_codes.split(",") if code]


def get_transaction_challenge(*, transaction: Transaction) -> Optional[TransactionChallenge]:
    """Return the challenge associated with one transaction when present."""

    try:
        return transaction.challenge
    except TransactionChallenge.DoesNotExist:
        return None


def expose_transaction_challenge_code() -> bool:
    """Return whether API responses may include the plain challenge code."""

    return settings.TRANSACTION_2FA_EXPOSE_CHALLENGE_CODE


def _resume_transaction_after_verified_challenge(*, transaction: Transaction) -> None:
    """Resume the accepted transaction flow after successful 2FA verification."""

    if transaction.transfer_type == Transaction.TransferType.INTERNAL:
        outbox = create_transaction_outbox(transaction=transaction)
        register_transaction_outbox_publish(outbox_id=outbox.id)


def _expire_transaction_challenge_if_due(
    *,
    transaction: Transaction,
    challenge: TransactionChallenge,
) -> None:
    """Mark a pending challenge as expired when its TTL has passed."""

    if challenge.status != TransactionChallenge.Status.PENDING:
        return
    if challenge.expires_at > timezone.now():
        return

    _fail_transaction_challenge(
        transaction=transaction,
        challenge=challenge,
        failure_reason="2FA challenge expired before confirmation.",
        status=TransactionChallenge.Status.EXPIRED,
    )


def _fail_transaction_challenge(
    *,
    transaction: Transaction,
    challenge: TransactionChallenge,
    failure_reason: str,
    status: str = TransactionChallenge.Status.FAILED,
) -> None:
    """Fail one transaction challenge and terminate the accepted transaction."""

    challenge.status = status
    challenge.save(update_fields=["status", "attempts_count", "updated_at"])
    transaction.status = Transaction.Status.FAILED
    transaction.completed_at = timezone.now()
    transaction.failure_reason = failure_reason
    transaction.save(update_fields=["status", "completed_at", "failure_reason"])
    FraudEvent.objects.create(
        user=transaction.initiated_by,
        transaction=transaction,
        event_type=FraudEvent.EventType.CHALLENGE,
        outcome=FraudEvent.Outcome.BLOCKED,
    )
    log_event(
        logger,
        logging.WARNING,
        "transaction.challenge_failed",
        message="Transaction challenge failed and the transaction was terminated.",
        transaction_id=transaction.id,
        user_id=transaction.initiated_by_id,
        status=transaction.status,
        transfer_type=transaction.transfer_type,
        failure_reason=failure_reason,
        challenge_id=challenge.id,
    )


def _generate_challenge_code() -> str:
    """Generate a zero-padded numeric 2FA code."""

    code_length = max(int(settings.TRANSACTION_2FA_CHALLENGE_CODE_LENGTH), 4)
    return str(secrets.randbelow(10**code_length)).zfill(code_length)
