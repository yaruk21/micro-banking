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
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
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
            from_account=first_account,
            to_account=recipient,
            amount=Decimal("10.00"),
            status=Transaction.Status.COMPLETED,
        )
        Transaction.objects.create(
            from_account=second_account,
            to_account=recipient,
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
            from_account=from_account,
            to_account=to_account,
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )

        response = self.client.get(
            reverse("transaction-status", kwargs={"pk": transaction.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], transaction.id)
        self.assertEqual(response.data["status"], Transaction.Status.PENDING)


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

        transfer = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("20.00"),
            )
        )
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

        transfer = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("25.00"),
            )
        )
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

        transfer = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("15.00"),
            )
        )
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

        transfer = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=account.iban,
                to_account_iban=account.iban,
                amount=Decimal("5.00"),
            )
        )
        process_transfer(transaction_id=transfer.id)
        account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(account.balance, Decimal("70.00"))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(transfer.status, Transaction.Status.FAILED)
        self.assertIn("must be different", transfer.failure_reason)
