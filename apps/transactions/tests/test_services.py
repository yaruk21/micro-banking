from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.accounts.services import SYSTEM_ACCOUNT_USERNAME
from apps.exchange.models import ExchangeRate
from apps.transactions.application import (
    TransferInput,
    TransactionPermissionError,
    TransactionValidationError,
    create_transfer,
    get_stuck_transaction_ids,
    process_transfer,
)
from apps.transactions.models import Transaction
from apps.transactions.workers.celery_tasks import recover_stuck_transfers_task

User = get_user_model()


class TransferServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="service-alice", password="pass123")
        self.other_user = User.objects.create_user(
            username="service-bob",
            password="pass123",
        )

    def test_create_transfer_success(self):
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

    def test_create_transfer_fails_with_insufficient_balance(self):
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

    def test_create_transfer_converts_amount_when_currency_differs(self):
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

    def test_create_transfer_fails_when_exchange_rate_is_missing(self):
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

    def test_create_transfer_fails_for_foreign_sender_account(self):
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

    def test_create_transfer_fails_for_same_account(self):
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

    def test_create_transfer_reuses_existing_transaction_for_same_idempotency_key(self):
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

    def test_get_stuck_transaction_ids_returns_only_stale_transactions(self):
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
    def test_recover_stuck_transfers_task_requeues_stale_transactions(
        self,
        mock_delay,
    ):
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
