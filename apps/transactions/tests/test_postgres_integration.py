from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.transactions.application import (
    IdempotencyConflictError,
    TransferInput,
    create_transfer,
    process_transfer,
)
from apps.transactions.models import Transaction

User = get_user_model()


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL test database")
class TransactionPostgresIntegrationTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="pg-alice",
            password="testpass123",
        )
        self.other_user = User.objects.create_user(
            username="pg-bob",
            password="testpass123",
        )

    def _run_concurrently(self, *callbacks):
        barrier = Barrier(len(callbacks))
        results = [None] * len(callbacks)

        def run_callback(index, callback):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                results[index] = callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [
                executor.submit(run_callback, index, callback)
                for index, callback in enumerate(callbacks)
            ]
            for future in futures:
                future.result(timeout=10)

        close_old_connections()
        return results

    def test_concurrent_create_transfer_reuses_single_transaction(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        def submit_transfer():
            transfer, created = create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("25.00"),
                    idempotency_key="pg-replay-1",
                )
            )
            return transfer.id, created

        results = self._run_concurrently(submit_transfer, submit_transfer)

        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(
            {transaction_id for transaction_id, _ in results},
            {Transaction.objects.get().id},
        )
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(sum(1 for _, created in results if not created), 1)

    def test_concurrent_create_transfer_with_different_payload_returns_conflict(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000022",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        def submit_transfer(amount):
            def callback():
                try:
                    transfer, created = create_transfer(
                        transfer_input=TransferInput(
                            user=self.user,
                            from_account_iban=from_account.iban,
                            to_account_iban=to_account.iban,
                            amount=amount,
                            idempotency_key="pg-replay-2",
                        )
                    )
                except IdempotencyConflictError:
                    return "conflict"
                return ("created", transfer.id, created)

            return callback

        results = self._run_concurrently(
            submit_transfer(Decimal("25.00")),
            submit_transfer(Decimal("30.00")),
        )

        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(sum(1 for result in results if result == "conflict"), 1)
        successful_results = [
            result
            for result in results
            if isinstance(result, tuple) and result[0] == "created"
        ]
        self.assertEqual(len(successful_results), 1)
        self.assertTrue(successful_results[0][2])

    def test_concurrent_process_transfer_applies_money_movement_once(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000031",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000032",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("30.00"),
                idempotency_key="pg-process-1",
            )
        )

        self.assertTrue(created)

        def process_existing_transfer():
            processed_transfer = process_transfer(transaction_id=transfer.id)
            return processed_transfer.status

        results = self._run_concurrently(
            process_existing_transfer,
            process_existing_transfer,
        )

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(
            results,
            [Transaction.Status.COMPLETED, Transaction.Status.COMPLETED],
        )
        self.assertEqual(transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(from_account.balance, Decimal("70.00"))
        self.assertEqual(to_account.balance, Decimal("30.00"))

    def test_concurrent_processing_prevents_double_spend(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000041",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        first_recipient = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000042",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        second_recipient = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000043",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        first_transfer, first_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=first_recipient.iban,
                amount=Decimal("40.00"),
                idempotency_key="pg-double-spend-1",
            )
        )
        second_transfer, second_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=second_recipient.iban,
                amount=Decimal("40.00"),
                idempotency_key="pg-double-spend-2",
            )
        )

        self.assertTrue(first_created)
        self.assertTrue(second_created)

        results = self._run_concurrently(
            lambda: process_transfer(transaction_id=first_transfer.id).status,
            lambda: process_transfer(transaction_id=second_transfer.id).status,
        )

        from_account.refresh_from_db()
        first_recipient.refresh_from_db()
        second_recipient.refresh_from_db()
        first_transfer.refresh_from_db()
        second_transfer.refresh_from_db()

        self.assertEqual(
            sum(1 for result in results if result == Transaction.Status.COMPLETED),
            1,
        )
        self.assertEqual(
            sum(1 for result in results if result == Transaction.Status.FAILED),
            1,
        )
        self.assertEqual(from_account.balance, Decimal("10.00"))
        self.assertEqual(
            sorted([first_recipient.balance, second_recipient.balance]),
            [Decimal("0.00"), Decimal("40.00")],
        )
        self.assertEqual(
            sorted([first_transfer.status, second_transfer.status]),
            [Transaction.Status.COMPLETED, Transaction.Status.FAILED],
        )
