from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.transactions.models import TransactionBatch

User = get_user_model()


class TransactionBatchApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="batch-alice",
            password="testpass123",
        )
        self.other_user = User.objects.create_user(
            username="batch-bob",
            password="testpass123",
        )
        self.client.force_authenticate(user=self.user)

    def _build_payload(self, from_account, to_account):
        return {
            "items": [
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "10.00",
                    "idempotency_key": "batch-item-1",
                },
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "15.00",
                    "idempotency_key": "batch-item-2",
                },
            ]
        }

    @patch("apps.transactions.workers.celery_tasks.process_transaction_batch_task.delay")
    def test_create_batch_returns_accepted_and_dispatches_worker(self, mock_delay):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBB00000000000000000000000000111",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBC00000000000000000000000000112",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("transaction-batch-create"),
                self._build_payload(from_account, to_account),
                HTTP_IDEMPOTENCY_KEY="batch-request-1",
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        batch = TransactionBatch.objects.get(id=response.data["id"])
        self.assertEqual(batch.total_items, 2)
        self.assertEqual(batch.items.count(), 2)
        mock_delay.assert_called_once_with(batch.id)

    @patch("apps.transactions.workers.celery_tasks.process_transaction_batch_task.delay")
    def test_same_batch_idempotency_key_returns_existing_batch(self, mock_delay):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBB00000000000000000000000000121",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBC00000000000000000000000000122",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        payload = self._build_payload(from_account, to_account)

        with self.captureOnCommitCallbacks(execute=True):
            first_response = self.client.post(
                reverse("transaction-batch-create"),
                payload,
                HTTP_IDEMPOTENCY_KEY="batch-request-2",
                format="json",
            )

        second_response = self.client.post(
            reverse("transaction-batch-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="batch-request-2",
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(TransactionBatch.objects.count(), 1)
        self.assertEqual(first_response.data["id"], second_response.data["id"])
        mock_delay.assert_called_once()

    @patch("apps.transactions.workers.celery_tasks.process_transaction_batch_task.delay")
    def test_same_batch_idempotency_key_with_different_payload_returns_conflict(
        self,
        mock_delay,
    ):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBB00000000000000000000000000131",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBC00000000000000000000000000132",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        with self.captureOnCommitCallbacks(execute=True):
            first_response = self.client.post(
                reverse("transaction-batch-create"),
                self._build_payload(from_account, to_account),
                HTTP_IDEMPOTENCY_KEY="batch-request-3",
                format="json",
            )

        second_payload = {
            "items": [
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "25.00",
                    "idempotency_key": "batch-item-3",
                }
            ]
        }
        second_response = self.client.post(
            reverse("transaction-batch-create"),
            second_payload,
            HTTP_IDEMPOTENCY_KEY="batch-request-3",
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(TransactionBatch.objects.count(), 1)
        mock_delay.assert_called_once()

    def test_batch_status_endpoint(self):
        batch = TransactionBatch.objects.create(
            initiated_by=self.user,
            idempotency_key="batch-status-1",
            request_fingerprint="batch-fingerprint-1",
            status=TransactionBatch.Status.PENDING,
            total_items=2,
        )

        response = self.client.get(
            reverse("transaction-batch-status", kwargs={"pk": batch.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], batch.id)
        self.assertEqual(response.data["status"], TransactionBatch.Status.PENDING)

    def test_batch_rejects_more_than_1000_items(self):
        payload = {
            "items": [
                {
                    "from_account_iban": f"FROM-{index}",
                    "to_account_iban": f"TO-{index}",
                    "amount": "1.00",
                    "idempotency_key": f"item-{index}",
                }
                for index in range(1001)
            ]
        }

        response = self.client.post(
            reverse("transaction-batch-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="batch-too-large-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
