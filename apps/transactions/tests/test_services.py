import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import storages
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.accounts.services import SYSTEM_ACCOUNT_USERNAME
from apps.exchange.models import ExchangeRate
from apps.transactions.application import (
    RequestFraudContext,
    SwiftTransferInput,
    TransferInput,
    confirm_transaction_challenge,
    create_transaction_report,
    TransactionFraudBlockedError,
    TransactionLimitExceededError,
    TransactionPermissionError,
    TransactionValidationError,
    create_swift_transfer,
    create_transfer,
    get_due_swift_transaction_ids,
    get_stuck_transaction_ids,
    process_transaction_report,
    process_transfer,
    process_swift_transfer,
)
from apps.transactions.models import (
    FraudEvent,
    SwiftTransferDetails,
    Transaction,
    TransactionChallenge,
    TransactionIdempotencyKey,
    TransactionOutbox,
    TransactionReport,
)
from apps.transactions.workers.celery_tasks import recover_stuck_transfers_task

User = get_user_model()


# Covers service-layer transfer behavior, including the standalone idempotency registry.
class TransferServiceTests(TestCase):
    """Validate transfer creation, processing, recovery, and idempotency behavior."""

    # Creates isolated users and resets caches before each service-layer test.
    def setUp(self):
        """Prepare a clean cache and baseline users for each test."""

        cache.clear()
        self.user = User.objects.create_user(username="service-alice", password="pass123")
        self.other_user = User.objects.create_user(
            username="service-bob",
            password="pass123",
        )

    # Verifies the happy-path same-currency transfer flow and registry persistence.
    def test_create_transfer_success(self):
        """A successful transfer should complete and persist one registry mapping."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("120.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("30.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("20.00"),
                idempotency_key="service-success-1",
            )
        )
        self.assertTrue(created)
        process_transfer(transaction_id=transfer.id)

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(from_account.balance, Decimal("100.00"))
        self.assertEqual(to_account.balance, Decimal("50.00"))
        self.assertEqual(transfer.exchange_rate, Decimal("1.00000000"))
        self.assertEqual(transfer.credited_amount, Decimal("20.00"))
        self.assertEqual(transfer.fee_amount, Decimal("0.00"))
        self.assertEqual(transfer.fee_currency, Account.Currency.USD)
        self.assertEqual(transfer.exchange_rate_provider, "internal")
        self.assertTrue(
            TransactionIdempotencyKey.objects.filter(
                initiated_by=self.user,
                idempotency_key="service-success-1",
                transaction_id=transfer.id,
            ).exists()
        )

    # Verifies that insufficient balance leads to a failed processed transaction.
    def test_create_transfer_fails_with_insufficient_balance(self):
        """Transfers without enough funds should fail without moving balances."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000022",
            currency=Account.Currency.USD,
            balance=Decimal("40.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("25.00"),
                idempotency_key="service-failed-1",
            )
        )
        self.assertTrue(created)
        process_transfer(transaction_id=transfer.id)
        from_account.refresh_from_db()
        to_account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(from_account.balance, Decimal("10.00"))
        self.assertEqual(to_account.balance, Decimal("40.00"))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(transfer.status, Transaction.Status.FAILED)

    # Verifies FX conversion, fee collection, and recipient crediting.
    def test_create_transfer_converts_amount_when_currency_differs(self):
        """Cross-currency transfers should apply exchange rates and fee routing."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000031",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000032",
            currency=Account.Currency.EUR,
            balance=Decimal("10.00"),
        )
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("40.00000000"),
            provider="privatbank",
            fetched_at=timezone.now(),
        )
        ExchangeRate.objects.create(
            base_currency="EUR",
            quote_currency="UAH",
            rate=Decimal("50.00000000"),
            provider="privatbank",
            fetched_at=timezone.now(),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("15.00"),
                idempotency_key="service-failed-2",
            )
        )
        self.assertTrue(created)
        process_transfer(transaction_id=transfer.id)
        from_account.refresh_from_db()
        to_account.refresh_from_db()
        transfer.refresh_from_db()
        fee_account = Account.objects.get(
            currency=Account.Currency.EUR,
            is_system=True,
        )
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(transfer.exchange_rate, Decimal("0.80000000"))
        self.assertEqual(transfer.credited_amount, Decimal("11.88"))
        self.assertEqual(transfer.fee_amount, Decimal("0.12"))
        self.assertEqual(transfer.fee_currency, Account.Currency.EUR)
        self.assertEqual(from_account.balance, Decimal("35.00"))
        self.assertEqual(to_account.balance, Decimal("21.88"))
        self.assertEqual(fee_account.balance, Decimal("0.12"))
        self.assertEqual(fee_account.owner.username, SYSTEM_ACCOUNT_USERNAME)

    # Verifies validation failure when no FX rate is available.
    def test_create_transfer_fails_when_exchange_rate_is_missing(self):
        """Cross-currency transfers should be rejected when the FX rate is missing."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000033",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000034",
            currency=Account.Currency.EUR,
            balance=Decimal("10.00"),
        )

        with self.assertRaisesMessage(
            TransactionValidationError,
            "Exchange rate for USD->EUR is not available.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("15.00"),
                    idempotency_key="service-failed-3",
                )
            )

    # Verifies ownership enforcement on the sender account.
    def test_create_transfer_fails_for_foreign_sender_account(self):
        """Users must not be able to create transfers from someone else's account."""

        from_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000041",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000042",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        with self.assertRaises(TransactionPermissionError):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="service-forbidden-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 0)

    # Verifies that self-transfers are accepted asynchronously but fail on processing.
    def test_create_transfer_fails_for_same_account(self):
        """Processing should fail when sender and receiver are the same account."""

        account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000051",
            currency=Account.Currency.USD,
            balance=Decimal("70.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=account.iban,
                to_account_iban=account.iban,
                amount=Decimal("5.00"),
                idempotency_key="service-same-account-1",
            )
        )
        self.assertTrue(created)
        process_transfer(transaction_id=transfer.id)
        account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(account.balance, Decimal("70.00"))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(transfer.status, Transaction.Status.FAILED)
        self.assertIn("must be different", transfer.failure_reason)

    # Verifies that the same key/payload pair reuses one transaction and one registry row.
    def test_create_transfer_reuses_existing_transaction_for_same_idempotency_key(self):
        """Repeated identical requests should resolve to the same transaction record."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000061",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000062",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        first_transfer, first_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="service-replay-1",
            )
        )
        second_transfer, second_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="service-replay-1",
            )
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_transfer.id, second_transfer.id)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(TransactionIdempotencyKey.objects.count(), 1)
        self.assertEqual(FraudEvent.objects.count(), 1)

    def test_create_transfer_stores_allowed_fraud_event(self):
        """A new transfer attempt should persist an allowed fraud event."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000069",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000070",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="service-fraud-allowed-1",
                fraud_context=RequestFraudContext(
                    request_id="req-fraud-1",
                    ip_address="203.0.113.10",
                    user_agent="test-agent/1.0",
                    country_code="ua",
                    region="Kyiv",
                    city="Kyiv",
                    latitude=Decimal("50.450100"),
                    longitude=Decimal("30.523400"),
                ),
            )
        )

        self.assertTrue(created)
        fraud_event = FraudEvent.objects.get()
        self.assertEqual(fraud_event.user_id, self.user.id)
        self.assertEqual(fraud_event.transaction_id, transfer.id)
        self.assertEqual(
            fraud_event.event_type,
            FraudEvent.EventType.TRANSACTION_ATTEMPT,
        )
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.ALLOWED)
        self.assertEqual(fraud_event.request_id, "req-fraud-1")
        self.assertEqual(fraud_event.ip_address, "203.0.113.10")
        self.assertEqual(fraud_event.country_code, "UA")

    @override_settings(
        TRANSACTION_2FA_CHALLENGE_AMOUNT=Decimal("30.00"),
        TRANSACTION_2FA_EXPOSE_CHALLENGE_CODE=True,
    )
    def test_create_transfer_requires_2fa_for_large_amount_and_resumes_after_confirm(self):
        """Large transfers should wait for 2FA and resume processing after confirmation."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000087",
            currency=Account.Currency.USD,
            balance=Decimal("120.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000088",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("40.00"),
                idempotency_key="service-2fa-large-1",
            )
        )

        self.assertTrue(created)
        self.assertEqual(transfer.status, Transaction.Status.PENDING)
        self.assertEqual(TransactionOutbox.objects.count(), 0)
        challenge = TransactionChallenge.objects.get(transaction=transfer)
        self.assertEqual(challenge.status, TransactionChallenge.Status.PENDING)
        self.assertTrue(getattr(transfer, "_challenge_code", ""))

        with self.captureOnCommitCallbacks(execute=True):
            confirmed_transfer = confirm_transaction_challenge(
                user=self.user,
                transaction_id=transfer.id,
                code=transfer._challenge_code,
            )

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        confirmed_transfer.refresh_from_db()
        challenge.refresh_from_db()

        self.assertEqual(challenge.status, TransactionChallenge.Status.VERIFIED)
        self.assertEqual(TransactionOutbox.objects.count(), 1)
        self.assertEqual(confirmed_transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(from_account.balance, Decimal("80.00"))
        self.assertEqual(to_account.balance, Decimal("60.00"))

    @override_settings(
        TRANSACTION_2FA_CHALLENGE_AMOUNT=Decimal("30.00"),
        TRANSACTION_2FA_CHALLENGE_MAX_ATTEMPTS=2,
    )
    def test_confirm_transaction_challenge_fails_after_too_many_invalid_codes(self):
        """Too many invalid 2FA codes should fail the challenge and transaction."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000089",
            currency=Account.Currency.USD,
            balance=Decimal("120.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000090",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("40.00"),
                idempotency_key="service-2fa-large-2",
            )
        )

        self.assertTrue(created)
        with self.assertRaisesMessage(TransactionValidationError, "Invalid 2FA code."):
            confirm_transaction_challenge(
                user=self.user,
                transaction_id=transfer.id,
                code="000000",
            )

        with self.assertRaisesMessage(
            TransactionValidationError,
            "2FA challenge failed after too many invalid attempts.",
        ):
            confirm_transaction_challenge(
                user=self.user,
                transaction_id=transfer.id,
                code="000000",
            )

        transfer.refresh_from_db()
        challenge = TransactionChallenge.objects.get(transaction=transfer)
        self.assertEqual(challenge.status, TransactionChallenge.Status.FAILED)
        self.assertEqual(transfer.status, Transaction.Status.FAILED)
        self.assertEqual(TransactionOutbox.objects.count(), 0)

    @override_settings(
        FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS=3,
        FRAUD_AMOUNT_ANOMALY_MULTIPLIER=Decimal("2.50"),
        FRAUD_AMOUNT_ACTION="flag",
    )
    def test_create_transfer_flags_amount_anomaly_above_user_baseline(self):
        """Behavioral fraud should flag unusually large amounts above user history."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000071",
            currency=Account.Currency.USD,
            balance=Decimal("500.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000072",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        for index, historical_amount in enumerate(("10.00", "12.00", "14.00"), start=1):
            Transaction.objects.create(
                initiated_by=self.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=f"amount-baseline-existing-{index}",
                request_fingerprint=f"amount-baseline-existing-{index}",
                amount=Decimal(historical_amount),
                status=Transaction.Status.COMPLETED,
                transfer_type=Transaction.TransferType.INTERNAL,
            )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("40.00"),
                idempotency_key="service-fraud-amount-1",
            )
        )

        self.assertTrue(created)
        fraud_event = FraudEvent.objects.latest("id")
        self.assertEqual(fraud_event.transaction_id, transfer.id)
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.FLAGGED)

    @override_settings(
        FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS=3,
        FRAUD_AMOUNT_ANOMALY_MULTIPLIER=Decimal("2.00"),
        FRAUD_AMOUNT_ACTION="flag",
    )
    def test_create_transfer_does_not_flag_amount_within_historical_maximum(self):
        """Behavioral fraud should not flag an amount that is already within prior maxima."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000083",
            currency=Account.Currency.USD,
            balance=Decimal("500.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000084",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        for index, historical_amount in enumerate(("10.00", "10.00", "35.00"), start=1):
            Transaction.objects.create(
                initiated_by=self.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=f"amount-max-existing-{index}",
                request_fingerprint=f"amount-max-existing-{index}",
                amount=Decimal(historical_amount),
                status=Transaction.Status.COMPLETED,
                transfer_type=Transaction.TransferType.INTERNAL,
            )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("30.00"),
                idempotency_key="service-fraud-amount-2",
            )
        )

        self.assertTrue(created)
        fraud_event = FraudEvent.objects.latest("id")
        self.assertEqual(fraud_event.transaction_id, transfer.id)
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.ALLOWED)

    @override_settings(
        FRAUD_FREQUENCY_WINDOW_SECONDS=60,
        FRAUD_FREQUENCY_MAX_ATTEMPTS=2,
        FRAUD_FREQUENCY_ACTION="block",
    )
    def test_create_transfer_blocks_when_frequency_threshold_is_exceeded(self):
        """Behavioral fraud should block bursts of transaction attempts when configured."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000073",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000074",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            request_id="prior-fraud-1",
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            request_id="prior-fraud-2",
        )

        with self.assertRaisesMessage(
            TransactionFraudBlockedError,
            "Transaction was blocked by behavioral fraud checks.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="service-fraud-block-1",
                    fraud_context=RequestFraudContext(request_id="req-fraud-block-1"),
                )
            )

        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(FraudEvent.objects.count(), 3)
        self.assertEqual(
            FraudEvent.objects.latest("id").outcome,
            FraudEvent.Outcome.BLOCKED,
        )

    @override_settings(
        FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS=3,
        FRAUD_AMOUNT_ANOMALY_MULTIPLIER=Decimal("2.50"),
        FRAUD_AMOUNT_ACTION="block",
    )
    def test_create_transfer_blocks_amount_anomaly_when_configured(self):
        """Behavioral fraud should block anomalously large amounts when configured."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000085",
            currency=Account.Currency.USD,
            balance=Decimal("500.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000086",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        for index, historical_amount in enumerate(("10.00", "12.00", "14.00"), start=1):
            Transaction.objects.create(
                initiated_by=self.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=f"amount-block-existing-{index}",
                request_fingerprint=f"amount-block-existing-{index}",
                amount=Decimal(historical_amount),
                status=Transaction.Status.COMPLETED,
                transfer_type=Transaction.TransferType.INTERNAL,
            )

        with self.assertRaisesMessage(
            TransactionFraudBlockedError,
            "Transaction was blocked by behavioral fraud checks.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("40.00"),
                    idempotency_key="service-fraud-amount-block-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 3)
        self.assertEqual(
            FraudEvent.objects.latest("id").outcome,
            FraudEvent.Outcome.BLOCKED,
        )

    @override_settings(
        FRAUD_GEO_COUNTRY_CHANGE_WINDOW_SECONDS=7200,
        FRAUD_GEO_ACTION="flag",
    )
    def test_create_transfer_flags_sharp_country_change(self):
        """Behavioral fraud should flag abrupt country changes across attempts."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000075",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000076",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            country_code="UA",
        )

        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="service-fraud-geo-1",
                fraud_context=RequestFraudContext(country_code="PL"),
            )
        )

        self.assertTrue(created)
        fraud_event = FraudEvent.objects.latest("id")
        self.assertEqual(fraud_event.transaction_id, transfer.id)
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.FLAGGED)

    @override_settings(TRANSACTION_SINGLE_LIMIT_AMOUNT=Decimal("9.99"))
    def test_create_transfer_rejects_single_transaction_limit_exceed(self):
        """Transfers above the configured single limit should be rejected."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000063",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000064",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        with self.assertRaisesMessage(
            TransactionLimitExceededError,
            "The single transaction limit is exceeded.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="service-single-limit-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 0)

    @override_settings(TRANSACTION_DAILY_LIMIT_AMOUNT=Decimal("30.00"))
    def test_create_transfer_rejects_daily_transaction_limit_exceed(self):
        """Transfers should be rejected when the configured daily limit is exceeded."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000065",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000066",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="daily-limit-existing-1",
            request_fingerprint="daily-limit-existing-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.COMPLETED,
            transfer_type=Transaction.TransferType.INTERNAL,
        )

        with self.assertRaisesMessage(
            TransactionLimitExceededError,
            "The daily transaction limit is exceeded.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="service-daily-limit-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 1)

    @override_settings(TRANSACTION_MONTHLY_LIMIT_AMOUNT=Decimal("50.00"))
    def test_create_transfer_rejects_monthly_transaction_limit_exceed(self):
        """Transfers should be rejected when the configured monthly limit is exceeded."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000067",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000068",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        current_month_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="monthly-limit-existing-1",
            request_fingerprint="monthly-limit-existing-1",
            amount=Decimal("45.00"),
            status=Transaction.Status.COMPLETED,
            transfer_type=Transaction.TransferType.INTERNAL,
        )
        old_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="monthly-limit-old-1",
            request_fingerprint="monthly-limit-old-1",
            amount=Decimal("45.00"),
            status=Transaction.Status.COMPLETED,
            transfer_type=Transaction.TransferType.INTERNAL,
        )
        previous_month = timezone.now().replace(day=1) - timedelta(days=1)
        Transaction.objects.filter(id=old_transaction.id).update(
            created_at=previous_month.replace(day=15)
        )

        with self.assertRaisesMessage(
            TransactionLimitExceededError,
            "The monthly transaction limit is exceeded.",
        ):
            create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="service-monthly-limit-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 2)

    # Verifies stale recovery lookup only returns records older than the threshold.
    def test_get_stuck_transaction_ids_returns_only_stale_transactions(self):
        """Recovery lookup should include only stale pending or processing transactions."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000071",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000072",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        stale_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="stale-tx-1",
            request_fingerprint="stale-fingerprint-1",
            amount=Decimal("10.00"),
            status=Transaction.Status.PENDING,
        )
        fresh_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="fresh-tx-1",
            request_fingerprint="fresh-fingerprint-1",
            amount=Decimal("12.00"),
            status=Transaction.Status.PENDING,
        )

        stale_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(id=stale_transaction.id).update(
            created_at=stale_time
        )

        stuck_ids = get_stuck_transaction_ids(threshold_seconds=300)

        self.assertEqual(stuck_ids, [stale_transaction.id])
        self.assertNotIn(fresh_transaction.id, stuck_ids)

    @patch("apps.transactions.workers.celery_tasks.process_transfer_task.delay")
    # Verifies that the Celery recovery task requeues stale transaction ids.
    def test_recover_stuck_transfers_task_requeues_stale_transactions(
        self,
        mock_delay,
    ):
        """Recovery task should re-dispatch stale transactions to Celery."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000081",
            currency=Account.Currency.USD,
            balance=Decimal("80.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBS00000000000000000000000000082",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )

        stale_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="recover-tx-1",
            request_fingerprint="recover-fingerprint-1",
            amount=Decimal("10.00"),
            status=Transaction.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(minutes=10),
        )

        recovered_count = recover_stuck_transfers_task()

        self.assertEqual(recovered_count, 1)
        mock_delay.assert_called_once_with(
            stale_transaction.id,
            correlation_id=None,
        )


class SwiftTransferServiceTests(TestCase):
    """Validate delayed SWIFT scheduling, pickup, and processing behavior."""

    def setUp(self):
        """Prepare a clean cache and baseline user for SWIFT tests."""

        cache.clear()
        self.user = User.objects.create_user(username="swift-alice", password="pass123")

    def _create_swift_transfer(self, *, amount: str = "25.00") -> Transaction:
        """Create one SWIFT transfer for service-layer tests."""

        unique_suffix = Account.objects.count() + 1
        from_account = Account.objects.create(
            owner=self.user,
            iban=f"MBS00000000000000000000000009{unique_suffix:03d}",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        transfer, created = create_swift_transfer(
            transfer_input=SwiftTransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                amount=Decimal(amount),
                idempotency_key=f"swift-service-{amount}-{unique_suffix}",
                swift_code="DEUTDEFF500",
                beneficiary_name="Alice Example",
                beneficiary_account_number="123456789",
                beneficiary_iban="DE89370400440532013000",
                beneficiary_bank_name="Deutsche Bank",
                beneficiary_bank_country="DE",
                beneficiary_address="Berlin, Germany",
                swift_reference="invoice-77",
            )
        )
        self.assertTrue(created)
        return transfer

    @patch(
        "apps.transactions.application.create._get_swift_completion_business_days",
        return_value=3,
    )
    def test_create_swift_transfer_schedules_next_business_days(
        self,
        mock_completion_days,
    ):
        """Friday submissions should skip the weekend for SWIFT planning dates."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000902",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        friday = datetime(2026, 5, 8, 12, 0, tzinfo=dt_timezone.utc)

        with patch("apps.transactions.application.create.timezone.now", return_value=friday):
            transfer, created = create_swift_transfer(
                transfer_input=SwiftTransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    amount=Decimal("25.00"),
                    idempotency_key="swift-schedule-1",
                    swift_code="DEUTDEFF500",
                    beneficiary_name="Alice Example",
                    beneficiary_account_number="123456789",
                    beneficiary_iban="DE89370400440532013000",
                    beneficiary_bank_name="Deutsche Bank",
                    beneficiary_bank_country="DE",
                    beneficiary_address="Berlin, Germany",
                    swift_reference="invoice-88",
                )
            )

        self.assertTrue(created)
        swift_details = transfer.swift_details
        self.assertEqual(
            swift_details.scheduled_processing_at,
            datetime(2026, 5, 11, 12, 0, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(
            swift_details.expected_completion_at,
            datetime(2026, 5, 13, 12, 0, tzinfo=dt_timezone.utc),
        )
        mock_completion_days.assert_called_once()

    def test_get_due_swift_transaction_ids_returns_due_pending_and_processing_transfers(self):
        """The due selector should return SWIFT items ready to start or finish."""

        due_transfer = self._create_swift_transfer(amount="25.00")
        completion_due_transfer = self._create_swift_transfer(amount="30.00")
        future_transfer = self._create_swift_transfer(amount="35.00")
        internal_sender = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000903",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        internal_recipient = Account.objects.create(
            owner=User.objects.create_user(username="swift-bob", password="pass123"),
            iban="MBS00000000000000000000000000904",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        internal_transfer = Transaction.objects.create(
            initiated_by=self.user,
            from_account=internal_sender,
            to_account=internal_recipient,
            idempotency_key="swift-due-internal",
            request_fingerprint="swift-due-internal",
            amount=Decimal("5.00"),
            status=Transaction.Status.PENDING,
            transfer_type=Transaction.TransferType.INTERNAL,
        )

        Transaction.objects.filter(id=due_transfer.id).update(status=Transaction.Status.PENDING)
        Transaction.objects.filter(id=completion_due_transfer.id).update(
            status=Transaction.Status.PROCESSING
        )
        SwiftTransferDetails.objects.filter(transaction_id=due_transfer.id).update(
            scheduled_processing_at=timezone.now() - timedelta(minutes=5)
        )
        SwiftTransferDetails.objects.filter(
            transaction_id=completion_due_transfer.id
        ).update(
            expected_completion_at=timezone.now() - timedelta(minutes=1)
        )
        SwiftTransferDetails.objects.filter(transaction_id=future_transfer.id).update(
            scheduled_processing_at=timezone.now() + timedelta(minutes=5)
        )

        due_ids = get_due_swift_transaction_ids(limit=10)

        self.assertEqual(len(due_ids), 2)
        self.assertCountEqual(
            due_ids,
            [due_transfer.id, completion_due_transfer.id],
        )
        self.assertNotIn(future_transfer.id, due_ids)
        self.assertNotIn(internal_transfer.id, due_ids)

    def test_process_swift_transfer_moves_due_transfer_to_processing_before_completion(
        self,
    ):
        """Due scheduled SWIFT transfer should enter processing before settlement day."""

        transfer = self._create_swift_transfer(amount="25.00")
        from_account = transfer.from_account
        SwiftTransferDetails.objects.filter(transaction_id=transfer.id).update(
            scheduled_processing_at=timezone.now() - timedelta(minutes=5),
            expected_completion_at=timezone.now() + timedelta(days=2),
        )

        processed_transfer = process_swift_transfer(transaction_id=transfer.id)
        from_account.refresh_from_db()
        processed_transfer.refresh_from_db()

        self.assertEqual(processed_transfer.status, Transaction.Status.PROCESSING)
        self.assertEqual(from_account.balance, Decimal("100.00"))
        self.assertIsNone(processed_transfer.completed_at)

    def test_process_swift_transfer_completes_due_processing_transfer_and_posts_fee(self):
        """Due SWIFT settlement should debit sender funds and credit the fee account."""

        transfer = self._create_swift_transfer(amount="25.00")
        SwiftTransferDetails.objects.filter(transaction_id=transfer.id).update(
            scheduled_processing_at=timezone.now() - timedelta(days=2),
            expected_completion_at=timezone.now() - timedelta(minutes=5),
        )
        Transaction.objects.filter(id=transfer.id).update(
            status=Transaction.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(days=1),
        )

        processed_transfer = process_swift_transfer(transaction_id=transfer.id)
        from_account = processed_transfer.from_account
        from_account.refresh_from_db()
        processed_transfer.refresh_from_db()
        fee_account = Account.objects.get(
            currency=Account.Currency.USD,
            is_system=True,
        )

        self.assertEqual(processed_transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(from_account.balance, Decimal("64.75"))
        self.assertEqual(fee_account.balance, Decimal("10.25"))
        self.assertIsNotNone(processed_transfer.completed_at)

    def test_process_swift_transfer_fails_when_balance_cannot_cover_fee(self):
        """SWIFT settlement should fail when sender funds cannot cover amount plus fee."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000905",
            currency=Account.Currency.USD,
            balance=Decimal("30.00"),
        )
        transfer, created = create_swift_transfer(
            transfer_input=SwiftTransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                amount=Decimal("25.00"),
                idempotency_key="swift-insufficient-1",
                swift_code="DEUTDEFF500",
                beneficiary_name="Alice Example",
                beneficiary_account_number="123456789",
                beneficiary_iban="DE89370400440532013000",
                beneficiary_bank_name="Deutsche Bank",
                beneficiary_bank_country="DE",
                beneficiary_address="Berlin, Germany",
                swift_reference="invoice-99",
            )
        )
        self.assertTrue(created)
        SwiftTransferDetails.objects.filter(transaction_id=transfer.id).update(
            scheduled_processing_at=timezone.now() - timedelta(days=2),
            expected_completion_at=timezone.now() - timedelta(minutes=5),
        )
        Transaction.objects.filter(id=transfer.id).update(
            status=Transaction.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(days=1),
        )

        processed_transfer = process_swift_transfer(transaction_id=transfer.id)
        from_account.refresh_from_db()
        processed_transfer.refresh_from_db()

        self.assertEqual(processed_transfer.status, Transaction.Status.FAILED)
        self.assertEqual(from_account.balance, Decimal("30.00"))
        self.assertIn("cover SWIFT amount and fee", processed_transfer.failure_reason)
        self.assertFalse(
            Account.objects.filter(
                currency=Account.Currency.USD,
                is_system=True,
            ).exists()
        )

    @override_settings(TRANSACTION_DAILY_LIMIT_AMOUNT=Decimal("30.00"))
    def test_create_swift_transfer_rejects_daily_transaction_limit_exceed(self):
        """SWIFT transfers should also respect the shared daily transaction limit."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000000999",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=None,
            idempotency_key="swift-daily-existing-1",
            request_fingerprint="swift-daily-existing-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
            transfer_type=Transaction.TransferType.SWIFT,
        )

        with self.assertRaisesMessage(
            TransactionLimitExceededError,
            "The daily transaction limit is exceeded.",
        ):
            create_swift_transfer(
                transfer_input=SwiftTransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    amount=Decimal("10.00"),
                    idempotency_key="swift-daily-limit-1",
                    swift_code="DEUTDEFF500",
                    beneficiary_name="Alice Example",
                    beneficiary_account_number="123456789",
                    beneficiary_iban="DE89370400440532013000",
                    beneficiary_bank_name="Deutsche Bank",
                    beneficiary_bank_country="DE",
                    beneficiary_address="Berlin, Germany",
                    swift_reference="invoice-limit-1",
                )
            )

        self.assertEqual(Transaction.objects.count(), 1)

    @override_settings(
        FRAUD_GEO_IMPOSSIBLE_TRAVEL_SPEED_KMH=600,
        FRAUD_GEO_ACTION="flag",
    )
    def test_create_swift_transfer_flags_impossible_travel(self):
        """SWIFT create should reuse the same behavioral fraud evaluation path."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000001000",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        prior_event = FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            country_code="UA",
            latitude=Decimal("50.450100"),
            longitude=Decimal("30.523400"),
        )
        FraudEvent.objects.filter(id=prior_event.id).update(
            created_at=timezone.now() - timedelta(minutes=30)
        )

        transfer, created = create_swift_transfer(
            transfer_input=SwiftTransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="swift-fraud-geo-1",
                swift_code="DEUTDEFF500",
                beneficiary_name="Alice Example",
                beneficiary_account_number="123456789",
                beneficiary_iban="DE89370400440532013000",
                beneficiary_bank_name="Deutsche Bank",
                beneficiary_bank_country="DE",
                beneficiary_address="Berlin, Germany",
                swift_reference="invoice-fraud-1",
                fraud_context=RequestFraudContext(
                    country_code="DE",
                    latitude=Decimal("52.520000"),
                    longitude=Decimal("13.405000"),
                ),
            )
        )

        self.assertTrue(created)
        fraud_event = FraudEvent.objects.latest("id")
        self.assertEqual(fraud_event.transaction_id, transfer.id)
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.FLAGGED)

    @override_settings(
        TRANSACTION_2FA_CHALLENGE_AMOUNT=Decimal("20.00"),
    )
    def test_swift_due_lookup_excludes_pending_2fa_challenges_until_verified(self):
        """SWIFT due pickup should ignore transactions that still await 2FA verification."""

        transfer = self._create_swift_transfer(amount="25.00")
        challenge = TransactionChallenge.objects.get(transaction=transfer)
        SwiftTransferDetails.objects.filter(transaction_id=transfer.id).update(
            scheduled_processing_at=timezone.now() - timedelta(minutes=5),
            expected_completion_at=timezone.now() + timedelta(days=1),
        )

        self.assertEqual(get_due_swift_transaction_ids(limit=10), [])

        challenge.status = TransactionChallenge.Status.VERIFIED
        challenge.verified_at = timezone.now()
        challenge.save(update_fields=["status", "verified_at", "updated_at"])

        self.assertEqual(get_due_swift_transaction_ids(limit=10), [transfer.id])


class TransactionReportServiceTests(TestCase):
    """Validate asynchronous transaction PDF report generation services."""

    def setUp(self):
        """Create baseline users and accounts for report generation."""

        cache.clear()
        self.report_storage_dir = tempfile.mkdtemp()
        report_storage_override = override_settings(
            STORAGES={
                **settings.STORAGES,
                "transaction_reports": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {
                        "location": self.report_storage_dir,
                        "base_url": "/media/transaction-reports/",
                    },
                },
            }
        )
        report_storage_override.enable()
        storages._storages.clear()
        self.addCleanup(report_storage_override.disable)
        self.addCleanup(storages._storages.clear)
        self.addCleanup(shutil.rmtree, self.report_storage_dir, ignore_errors=True)
        self.user = User.objects.create_user(
            username="report-alice",
            password="pass123",
        )
        self.other_user = User.objects.create_user(
            username="report-bob",
            password="pass123",
        )

    @patch("apps.transactions.application.reporting._dispatch_transaction_report")
    def test_process_transaction_report_generates_completed_pdf(
        self,
        mock_dispatch_report,
    ):
        """Processing a pending report should persist a completed stored PDF."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBR00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("150.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBR00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("20.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="report-service-1",
            request_fingerprint="report-service-1",
            amount=Decimal("25.00"),
            credited_amount=Decimal("25.00"),
            status=Transaction.Status.COMPLETED,
        )
        Transaction.objects.filter(id=transaction.id).update(
            created_at="2026-05-05T10:00:00Z"
        )

        report = create_transaction_report(
            user=self.user,
            date_from=timezone.datetime(2026, 5, 5).date(),
            date_to=timezone.datetime(2026, 5, 5).date(),
        )
        generated_report = process_transaction_report(report_id=report.id)

        report.refresh_from_db()

        self.assertEqual(report.status, TransactionReport.Status.COMPLETED)
        self.assertEqual(generated_report.status, TransactionReport.Status.COMPLETED)
        self.assertEqual(report.content_type, "application/pdf")
        self.assertTrue(report.file_name.endswith(".pdf"))
        self.assertTrue(report.storage_key)
        self.assertIsNone(report.pdf_content)
        self.assertTrue(storages["transaction_reports"].exists(report.storage_key))
        mock_dispatch_report.assert_not_called()
