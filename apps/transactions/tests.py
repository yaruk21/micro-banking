from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account

from .models import Transaction
from .services import (
    TransferInput,
    TransactionPermissionError,
    create_transfer,
    process_transfer,
)

User = get_user_model()


class TransactionApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="testpass123")
        self.other_user = User.objects.create_user(username="bob", password="testpass123")

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "alice", "password": "testpass123"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_response.data['access']}"
        )

    def test_successful_transfer(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "25.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-success-1",
            format="json",
        )

        from_account.refresh_from_db()
        to_account.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(from_account.balance, Decimal("75.00"))
        self.assertEqual(to_account.balance, Decimal("35.00"))
        self.assertEqual(
            Transaction.objects.get().status,
            Transaction.Status.COMPLETED,
        )

    def test_transfer_fails_with_insufficient_balance(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("5.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000022",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "25.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-failed-1",
            format="json",
        )

        from_account.refresh_from_db()
        to_account.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(from_account.balance, Decimal("5.00"))
        self.assertEqual(to_account.balance, Decimal("0.00"))
        self.assertEqual(Transaction.objects.get().status, Transaction.Status.FAILED)

    def test_transfer_fails_for_foreign_sender_account(self):
        from_account = Account.objects.create(
            owner=self.other_user,
            iban="MBA00000000000000000000000000031",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.user,
            iban="MBB00000000000000000000000000032",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "10.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-forbidden-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_transfer_requires_idempotency_key_header(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000035",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000036",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "10.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "Idempotency-Key header is required.",
        )
        self.assertEqual(Transaction.objects.count(), 0)

    def test_transaction_list_filter_by_account(self):
        first_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000041",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        second_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000042",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        recipient = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000043",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        first_transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=first_account,
            to_account=recipient,
            idempotency_key="list-filter-1",
            request_fingerprint="fingerprint-list-filter-1",
            amount=Decimal("10.00"),
            status=Transaction.Status.COMPLETED,
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=second_account,
            to_account=recipient,
            idempotency_key="list-filter-2",
            request_fingerprint="fingerprint-list-filter-2",
            amount=Decimal("15.00"),
            status=Transaction.Status.COMPLETED,
        )

        response = self.client.get(
            reverse("transaction-list-create"),
            {"account": first_account.id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], first_transaction.id)

    def test_transaction_status_endpoint(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000051",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000052",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="status-view-1",
            request_fingerprint="fingerprint-status-view-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )

        response = self.client.get(
            reverse("transaction-status", kwargs={"pk": transaction.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], transaction.id)
        self.assertEqual(response.data["status"], Transaction.Status.PENDING)

    def test_same_idempotency_key_returns_existing_transaction(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000061",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000062",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        payload = {
            "from_account_iban": from_account.iban,
            "to_account_iban": to_account.iban,
            "amount": "25.00",
        }

        first_response = self.client.post(
            reverse("transaction-list-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="replay-key-1",
            format="json",
        )
        second_response = self.client.post(
            reverse("transaction-list-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="replay-key-1",
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(first_response.data["id"], second_response.data["id"])

    def test_same_idempotency_key_with_different_payload_returns_conflict(self):
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000071",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000072",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        first_response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "25.00",
            },
            HTTP_IDEMPOTENCY_KEY="replay-key-2",
            format="json",
        )
        second_response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "30.00",
            },
            HTTP_IDEMPOTENCY_KEY="replay-key-2",
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Transaction.objects.count(), 1)


class TransferServiceTests(TestCase):
    def setUp(self):
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

    def test_create_transfer_fails_when_currency_differs(self):
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
        transfer.refresh_from_db()
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(transfer.status, Transaction.Status.FAILED)
        self.assertIn("same currency", transfer.failure_reason)

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
