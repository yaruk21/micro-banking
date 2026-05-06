from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import Account
from apps.transactions.models import Transaction, SwiftTransferDetails

User = get_user_model()


class SwiftTransferDetailsModelTests(TestCase):
    """Validate SWIFT transfer metadata persistence and validation rules."""

    def setUp(self):
        """Create a baseline transfer that can own SWIFT metadata."""

        self.user = User.objects.create_user(
            username="swift-model-user",
            password="pass123",
        )
        self.recipient = User.objects.create_user(
            username="swift-model-recipient",
            password="pass123",
        )
        self.from_account = Account.objects.create(
            owner=self.user,
            iban="MBS00000000000000000000000001001",
            currency=Account.Currency.USD,
            balance=Decimal("250.00"),
        )
        self.to_account = Account.objects.create(
            owner=self.recipient,
            iban="MBS00000000000000000000000001002",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        self.transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=self.from_account,
            to_account=self.to_account,
            idempotency_key="swift-model-1",
            request_fingerprint="swift-model-fingerprint",
            amount=Decimal("25.00"),
            credited_amount=Decimal("25.00"),
            exchange_rate=Decimal("1.00000000"),
            exchange_rate_provider="internal",
            fee_amount=Decimal("0.00"),
            fee_currency=Account.Currency.USD,
            status=Transaction.Status.PENDING,
        )

    def test_save_normalizes_swift_fields(self):
        """Canonical SWIFT fields should be normalized before persistence."""

        details = SwiftTransferDetails.objects.create(
            transaction=self.transaction,
            swift_code="deutdeff500",
            beneficiary_name="Alice Example",
            beneficiary_account_number=" 1234 5678 ",
            beneficiary_iban=" de89370400440532013000 ",
            beneficiary_bank_name="Deutsche Bank",
            beneficiary_bank_country="de",
            beneficiary_address="Berlin, Germany",
            swift_reference=" invoice-42 ",
        )

        self.assertEqual(details.swift_code, "DEUTDEFF500")
        self.assertEqual(details.beneficiary_account_number, "12345678")
        self.assertEqual(details.beneficiary_iban, "DE89370400440532013000")
        self.assertEqual(details.beneficiary_bank_country, "DE")
        self.assertEqual(details.swift_reference, "invoice-42")
        self.assertEqual(str(details), f"swift:{self.transaction.id}:DEUTDEFF500")

    def test_full_clean_rejects_invalid_swift_code(self):
        """Model validation should reject malformed SWIFT/BIC values."""

        details = SwiftTransferDetails(
            transaction=self.transaction,
            swift_code="bad-code",
            beneficiary_name="Alice Example",
            beneficiary_account_number="12345678",
            beneficiary_iban="DE89370400440532013000",
            beneficiary_bank_name="Deutsche Bank",
            beneficiary_bank_country="DE",
        )

        with self.assertRaises(ValidationError):
            details.full_clean()

    def test_swift_transaction_can_omit_to_account(self):
        """SWIFT transactions may target an external beneficiary instead of a local account."""

        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=self.from_account,
            to_account=None,
            idempotency_key="swift-model-2",
            request_fingerprint="swift-model-fingerprint-2",
            amount=Decimal("40.00"),
            credited_amount=Decimal("40.00"),
            exchange_rate=Decimal("1.00000000"),
            exchange_rate_provider="internal",
            fee_amount=Decimal("10.00"),
            fee_currency=Account.Currency.USD,
            status=Transaction.Status.PENDING,
            transfer_type=Transaction.TransferType.SWIFT,
        )

        self.assertIsNone(transaction.to_account)
        self.assertEqual(
            str(transaction),
            f"{self.from_account.id}->external:40.00/40.00",
        )
