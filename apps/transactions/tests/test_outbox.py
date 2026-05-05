from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Account
from apps.transactions.application import (
    TransferInput,
    create_transfer,
    publish_pending_transaction_outbox,
    publish_transaction_outbox,
)
from apps.transactions.models import TransactionOutbox

User = get_user_model()


class TransactionOutboxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="outbox-alice",
            password="pass123",
        )
        self.other_user = User.objects.create_user(
            username="outbox-bob",
            password="pass123",
        )
        self.from_account = Account.objects.create(
            owner=self.user,
            iban="MBO00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        self.to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBO00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

    def test_create_transfer_creates_pending_outbox_entry(self):
        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=self.from_account.iban,
                to_account_iban=self.to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="outbox-create-1",
            )
        )

        self.assertTrue(created)
        outbox = TransactionOutbox.objects.get(transaction=transfer)
        self.assertEqual(outbox.delivery_attempts, 0)
        self.assertIsNone(outbox.published_at)
        self.assertEqual(outbox.celery_task_id, "")

    @patch("apps.transactions.workers.celery_tasks.process_transfer_task.delay")
    def test_publish_transaction_outbox_marks_entry_as_dispatched(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="celery-dispatch-1")
        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=self.from_account.iban,
                to_account_iban=self.to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="outbox-publish-1",
            )
        )

        self.assertTrue(created)
        outbox = TransactionOutbox.objects.get(transaction=transfer)

        was_published = publish_transaction_outbox(outbox_id=outbox.id)

        outbox.refresh_from_db()
        self.assertTrue(was_published)
        self.assertEqual(outbox.delivery_attempts, 1)
        self.assertEqual(outbox.celery_task_id, "celery-dispatch-1")
        self.assertIsNotNone(outbox.published_at)
        mock_delay.assert_called_once_with(transfer.id, correlation_id=None)

    @patch("apps.transactions.workers.celery_tasks.process_transfer_task.delay")
    def test_publish_transaction_outbox_persists_dispatch_error(self, mock_delay):
        mock_delay.side_effect = RuntimeError("broker unavailable")
        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=self.from_account.iban,
                to_account_iban=self.to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="outbox-publish-2",
            )
        )

        self.assertTrue(created)
        outbox = TransactionOutbox.objects.get(transaction=transfer)

        was_published = publish_transaction_outbox(outbox_id=outbox.id)

        outbox.refresh_from_db()
        self.assertFalse(was_published)
        self.assertEqual(outbox.delivery_attempts, 1)
        self.assertIsNone(outbox.published_at)
        self.assertEqual(outbox.last_error, "broker unavailable")

    @patch("apps.transactions.workers.celery_tasks.process_transfer_task.delay")
    def test_publish_pending_transaction_outbox_dispatches_only_pending_entries(
        self,
        mock_delay,
    ):
        mock_delay.side_effect = [
            SimpleNamespace(id="celery-batch-1"),
            SimpleNamespace(id="celery-batch-2"),
        ]
        first_transfer, _ = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=self.from_account.iban,
                to_account_iban=self.to_account.iban,
                amount=Decimal("10.00"),
                idempotency_key="outbox-batch-1",
            )
        )
        second_transfer, _ = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=self.from_account.iban,
                to_account_iban=self.to_account.iban,
                amount=Decimal("11.00"),
                idempotency_key="outbox-batch-2",
            )
        )
        first_outbox = TransactionOutbox.objects.get(transaction=first_transfer)
        second_outbox = TransactionOutbox.objects.get(transaction=second_transfer)

        published_count = publish_pending_transaction_outbox(limit=10)

        first_outbox.refresh_from_db()
        second_outbox.refresh_from_db()
        self.assertEqual(published_count, 2)
        self.assertIsNotNone(first_outbox.published_at)
        self.assertIsNotNone(second_outbox.published_at)
