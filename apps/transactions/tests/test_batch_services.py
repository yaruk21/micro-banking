from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Account
from apps.transactions.application import process_transaction_batch
from apps.transactions.models import (
    Transaction,
    TransactionBatch,
    TransactionBatchItem,
)

User = get_user_model()


class TransactionBatchServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="batch-service-alice",
            password="pass123",
        )
        self.other_user = User.objects.create_user(
            username="batch-service-bob",
            password="pass123",
        )

    def test_process_transaction_batch_creates_transactions_and_updates_counts(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBD00000000000000000000000000111",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBE00000000000000000000000000112",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        batch = TransactionBatch.objects.create(
            initiated_by=self.user,
            idempotency_key="service-batch-1",
            request_fingerprint="service-batch-fingerprint-1",
            status=TransactionBatch.Status.PENDING,
            total_items=2,
        )
        TransactionBatchItem.objects.create(
            batch=batch,
            sequence=1,
            from_account_iban=from_account.iban,
            to_account_iban=to_account.iban,
            amount=Decimal("10.00"),
            idempotency_key="service-batch-item-1",
        )
        TransactionBatchItem.objects.create(
            batch=batch,
            sequence=2,
            from_account_iban=from_account.iban,
            to_account_iban=to_account.iban,
            amount=Decimal("15.00"),
            idempotency_key="service-batch-item-2",
        )

        processed_batch = process_transaction_batch(batch_id=batch.id)

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        processed_batch.refresh_from_db()
        self.assertEqual(processed_batch.status, TransactionBatch.Status.COMPLETED)
        self.assertEqual(processed_batch.processed_items, 2)
        self.assertEqual(processed_batch.succeeded_items, 2)
        self.assertEqual(processed_batch.failed_items, 0)
        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(from_account.balance, Decimal("100.00"))
        self.assertEqual(to_account.balance, Decimal("0.00"))
        self.assertEqual(
            sorted(
                Transaction.objects.values_list("status", flat=True)
            ),
            [Transaction.Status.PENDING, Transaction.Status.PENDING],
        )

    def test_process_transaction_batch_records_item_level_errors(self):
        batch = TransactionBatch.objects.create(
            initiated_by=self.user,
            idempotency_key="service-batch-2",
            request_fingerprint="service-batch-fingerprint-2",
            status=TransactionBatch.Status.PENDING,
            total_items=1,
        )
        TransactionBatchItem.objects.create(
            batch=batch,
            sequence=1,
            from_account_iban="UNKNOWN-FROM",
            to_account_iban="UNKNOWN-TO",
            amount=Decimal("10.00"),
            idempotency_key="service-batch-item-3",
        )

        processed_batch = process_transaction_batch(batch_id=batch.id)
        item = processed_batch.items.get()

        processed_batch.refresh_from_db()
        self.assertEqual(processed_batch.status, TransactionBatch.Status.COMPLETED)
        self.assertEqual(processed_batch.processed_items, 1)
        self.assertEqual(processed_batch.succeeded_items, 0)
        self.assertEqual(processed_batch.failed_items, 1)
        self.assertIn("Both accounts must exist.", item.error_message)
