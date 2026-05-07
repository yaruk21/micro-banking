import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.accounts.services import SYSTEM_ACCOUNT_USERNAME
from apps.exchange.models import ExchangeRate
from core.cache_utils import bump_user_cache_version
from apps.transactions.models import (
    FraudEvent,
    SwiftTransferDetails,
    Transaction,
    TransactionChallenge,
    TransactionOutbox,
    TransactionReport,
)
from apps.transactions.selectors import list_user_transactions

User = get_user_model()


class FakeCloudReportStorage:
    """In-memory storage with presigned-like URL support for report tests."""

    def __init__(self, **kwargs):
        self.files = {}

    def save(self, name, content):
        content.open("rb")
        self.files[name] = content.read()
        return name

    def open(self, name, mode="rb"):
        if name not in self.files:
            raise OSError("missing object")
        from io import BytesIO

        return BytesIO(self.files[name])

    def exists(self, name):
        return name in self.files

    def delete(self, name):
        self.files.pop(name, None)

    def url(self, name, parameters=None, expire=None):
        return f"https://reports.example.test/{name}?expires={expire or 0}"


class TransactionApiTests(APITestCase):
    """Test transaction api test behavior."""
    def setUp(self):
        """Handle set up."""
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
        self.user = User.objects.create_user(username="alice", password="testpass123")
        self.other_user = User.objects.create_user(username="bob", password="testpass123")
        self.client.force_authenticate(user=self.user)

    def test_successful_transfer(self):
        """Test that successful transfer."""
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

        with self.captureOnCommitCallbacks(execute=True):
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
        self.assertEqual(
            Transaction.objects.get().credited_amount,
            Decimal("25.00"),
        )
        self.assertEqual(
            Transaction.objects.get().fee_amount,
            Decimal("0.00"),
        )

    def test_cross_currency_transfer_uses_exchange_rate(self):
        """Test that cross currency transfer uses exchange rate."""
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000022",
            currency=Account.Currency.EUR,
            balance=Decimal("10.00"),
        )
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("40.00000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )
        ExchangeRate.objects.create(
            base_currency="EUR",
            quote_currency="UAH",
            rate=Decimal("50.00000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("transaction-list-create"),
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "25.00",
                },
                HTTP_IDEMPOTENCY_KEY="txn-fx-1",
                format="json",
            )

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        fee_account = Account.objects.get(
            currency=Account.Currency.EUR,
            is_system=True,
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(from_account.balance, Decimal("75.00"))
        self.assertEqual(to_account.balance, Decimal("29.80"))
        self.assertEqual(fee_account.balance, Decimal("0.20"))
        self.assertEqual(fee_account.owner.username, SYSTEM_ACCOUNT_USERNAME)
        self.assertEqual(Transaction.objects.get().status, Transaction.Status.COMPLETED)
        self.assertEqual(response.data["credited_amount"], "19.80")
        self.assertEqual(response.data["exchange_rate"], "0.80000000")
        self.assertEqual(response.data["fee_amount"], "0.20")
        self.assertEqual(response.data["fee_currency"], "EUR")

    def test_transfer_fails_with_insufficient_balance(self):
        """Test that transfer fails with insufficient balance."""
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000023",
            currency=Account.Currency.USD,
            balance=Decimal("5.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000024",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        with self.captureOnCommitCallbacks(execute=True):
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

    def test_transaction_analytics_summary_returns_status_totals_and_cashflow(self):
        """Analytics summary should aggregate user-visible transaction activity."""

        own_usd_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000101",
            currency=Account.Currency.USD,
            balance=Decimal("1000.00"),
        )
        own_eur_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000102",
            currency=Account.Currency.EUR,
            balance=Decimal("1000.00"),
        )
        bob_usd_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000103",
            currency=Account.Currency.USD,
            balance=Decimal("1000.00"),
        )

        outgoing_completed = Transaction.objects.create(
            initiated_by=self.user,
            from_account=own_usd_account,
            to_account=bob_usd_account,
            idempotency_key="analytics-outgoing-completed",
            request_fingerprint="analytics-outgoing-completed",
            amount=Decimal("25.00"),
            credited_amount=Decimal("25.00"),
            status=Transaction.Status.COMPLETED,
        )
        incoming_completed = Transaction.objects.create(
            initiated_by=self.other_user,
            from_account=bob_usd_account,
            to_account=own_eur_account,
            idempotency_key="analytics-incoming-completed",
            request_fingerprint="analytics-incoming-completed",
            amount=Decimal("50.00"),
            credited_amount=Decimal("40.00"),
            exchange_rate=Decimal("0.80000000"),
            status=Transaction.Status.COMPLETED,
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=own_eur_account,
            to_account=None,
            idempotency_key="analytics-swift-failed",
            request_fingerprint="analytics-swift-failed",
            amount=Decimal("70.00"),
            status=Transaction.Status.FAILED,
            transfer_type=Transaction.TransferType.SWIFT,
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=own_usd_account,
            to_account=bob_usd_account,
            idempotency_key="analytics-pending",
            request_fingerprint="analytics-pending",
            amount=Decimal("10.00"),
            status=Transaction.Status.PENDING,
        )
        Transaction.objects.filter(id=outgoing_completed.id).update(
            created_at="2026-05-05T10:00:00Z"
        )
        Transaction.objects.filter(id=incoming_completed.id).update(
            created_at="2026-05-06T10:00:00Z"
        )

        response = self.client.get(
            reverse("transaction-analytics-summary"),
            {
                "date_from": "2026-05-05",
                "date_to": "2026-05-06",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["totals"]["total_transactions"], 2)
        self.assertEqual(response.data["totals"]["completed_transactions"], 2)
        self.assertEqual(response.data["totals"]["pending_transactions"], 0)
        self.assertEqual(response.data["totals"]["failed_transactions"], 0)
        self.assertEqual(response.data["totals"]["completed_outgoing_amount"], "25.00")
        self.assertEqual(response.data["totals"]["completed_incoming_amount"], "40.00")
        self.assertEqual(response.data["totals"]["net_completed_cashflow"], "15.00")
        self.assertEqual(
            response.data["by_currency"],
            [
                {
                    "currency": "EUR",
                    "outgoing_transactions": 0,
                    "incoming_transactions": 1,
                    "completed_outgoing_amount": "0.00",
                    "completed_incoming_amount": "40.00",
                    "net_completed_cashflow": "40.00",
                },
                {
                    "currency": "USD",
                    "outgoing_transactions": 1,
                    "incoming_transactions": 0,
                    "completed_outgoing_amount": "25.00",
                    "completed_incoming_amount": "0.00",
                    "net_completed_cashflow": "-25.00",
                },
            ],
        )
        self.assertEqual(
            response.data["by_transfer_type"],
            [
                {
                    "transfer_type": Transaction.TransferType.INTERNAL,
                    "total_transactions": 2,
                    "completed_transactions": 2,
                    "total_amount": "75.00",
                }
            ],
        )

    def test_transaction_analytics_summary_uses_cache_until_version_bump(self):
        """Analytics summary should reuse cached payload until transaction cache version changes."""

        own_usd_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000111",
            currency=Account.Currency.USD,
            balance=Decimal("1000.00"),
        )
        bob_usd_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000112",
            currency=Account.Currency.USD,
            balance=Decimal("1000.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=own_usd_account,
            to_account=bob_usd_account,
            idempotency_key="analytics-cache-1",
            request_fingerprint="analytics-cache-1",
            amount=Decimal("25.00"),
            credited_amount=Decimal("25.00"),
            status=Transaction.Status.COMPLETED,
        )

        url = reverse("transaction-analytics-summary")
        first_response = self.client.get(url)
        Transaction.objects.filter(id=transaction.id).update(amount=Decimal("40.00"))
        second_response = self.client.get(url)
        bump_user_cache_version(namespace="transactions_list", user_id=self.user.id)
        third_response = self.client.get(url)

        self.assertEqual(
            first_response.data["totals"]["completed_outgoing_amount"],
            "25.00",
        )
        self.assertEqual(
            second_response.data["totals"]["completed_outgoing_amount"],
            "25.00",
        )
        self.assertEqual(
            third_response.data["totals"]["completed_outgoing_amount"],
            "40.00",
        )

    def test_transaction_analytics_summary_rejects_invalid_date_range(self):
        """Analytics summary should validate the requested date range."""

        response = self.client.get(
            reverse("transaction-analytics-summary"),
            {
                "date_from": "2026-05-07",
                "date_to": "2026-05-06",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("date_to", response.data)

    @patch("apps.transactions.application.reporting._dispatch_transaction_report")
    def test_transaction_report_create_endpoint_queues_background_generation(
        self,
        mock_dispatch_report,
    ):
        """Report create endpoint should persist a pending report and dispatch async work."""

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("transaction-report-create"),
                {
                    "date_from": "2026-05-01",
                    "date_to": "2026-05-07",
                },
                format="json",
            )

        report = TransactionReport.objects.get()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(report.status, TransactionReport.Status.PENDING)
        self.assertEqual(str(report.date_from), "2026-05-01")
        self.assertEqual(str(report.date_to), "2026-05-07")
        mock_dispatch_report.assert_called_once_with(report_id=report.id)

    def test_transaction_report_status_and_download_endpoints(self):
        """Completed reports should expose a temporary link and downloadable stored PDF."""

        storage_key = storages["transaction_reports"].save(
            "user-1/report-1/transaction-report-test.pdf",
            ContentFile(b"%PDF-1.4\nreport\n"),
        )

        report = TransactionReport.objects.create(
            user=self.user,
            date_from=timezone.datetime(2026, 5, 1).date(),
            date_to=timezone.datetime(2026, 5, 7).date(),
            status=TransactionReport.Status.COMPLETED,
            file_name="transaction-report-test.pdf",
            storage_key=storage_key,
            completed_at=timezone.now(),
        )

        status_response = self.client.get(
            reverse("transaction-report-status", kwargs={"pk": report.id})
        )

        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertEqual(status_response.data["status"], TransactionReport.Status.COMPLETED)
        self.assertIn("/api/transactions/reports/", status_response.data["download_url"])
        self.assertIn("token=", status_response.data["download_url"])

        download_url = urlsplit(status_response.data["download_url"])
        self.client.force_authenticate(user=None)
        download_response = self.client.get(
            f"{download_url.path}?{download_url.query}"
        )

        self.assertEqual(download_response.status_code, status.HTTP_200_OK)
        self.assertEqual(download_response["Content-Type"], "application/pdf")
        self.assertIn(
            'attachment; filename="transaction-report-test.pdf"',
            download_response["Content-Disposition"],
        )
        self.assertEqual(
            b"".join(download_response.streaming_content),
            b"%PDF-1.4\nreport\n",
        )

    def test_transaction_report_download_endpoint_keeps_legacy_inline_pdf_support(self):
        """Legacy inline PDF payloads should stay downloadable for the owner."""

        report = TransactionReport.objects.create(
            user=self.user,
            date_from=timezone.datetime(2026, 5, 1).date(),
            date_to=timezone.datetime(2026, 5, 7).date(),
            status=TransactionReport.Status.COMPLETED,
            file_name="transaction-report-legacy.pdf",
            pdf_content=b"%PDF-1.4\nlegacy-report\n",
            completed_at=timezone.now(),
        )

        response = self.client.get(
            reverse("transaction-report-download", kwargs={"pk": report.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"%PDF-1.4\nlegacy-report\n")

    @override_settings(
        STORAGES={
            **settings.STORAGES,
            "transaction_reports": {
                "BACKEND": "apps.transactions.tests.test_api.FakeCloudReportStorage",
            },
        }
    )
    def test_transaction_report_download_endpoint_redirects_to_cloud_storage_url(self):
        """Cloud-backed report downloads should redirect to a temporary storage URL."""

        storages._storages.clear()
        storage_key = storages["transaction_reports"].save(
            "user-1/report-9/transaction-report-cloud.pdf",
            ContentFile(b"%PDF-1.4\ncloud-report\n"),
        )
        report = TransactionReport.objects.create(
            user=self.user,
            date_from=timezone.datetime(2026, 5, 1).date(),
            date_to=timezone.datetime(2026, 5, 7).date(),
            status=TransactionReport.Status.COMPLETED,
            file_name="transaction-report-cloud.pdf",
            storage_key=storage_key,
            completed_at=timezone.now(),
        )

        status_response = self.client.get(
            reverse("transaction-report-status", kwargs={"pk": report.id})
        )
        download_url = urlsplit(status_response.data["download_url"])
        self.client.force_authenticate(user=None)
        response = self.client.get(
            f"{download_url.path}?{download_url.query}",
            follow=False,
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("https://reports.example.test/", response["Location"])

    def test_transaction_report_download_endpoint_returns_conflict_while_pending(self):
        """Pending reports should not be downloadable yet."""

        report = TransactionReport.objects.create(
            user=self.user,
            date_from=timezone.datetime(2026, 5, 1).date(),
            date_to=timezone.datetime(2026, 5, 7).date(),
            status=TransactionReport.Status.PENDING,
            file_name="transaction-report-pending.pdf",
        )

        response = self.client.get(
            reverse("transaction-report-download", kwargs={"pk": report.id})
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data["detail"],
            "Transaction report is not ready for download.",
        )

    @override_settings(
        TRANSACTION_2FA_CHALLENGE_AMOUNT=Decimal("30.00"),
        TRANSACTION_2FA_EXPOSE_CHALLENGE_CODE=True,
    )
    def test_large_transfer_requires_2fa_and_confirm_endpoint_resumes_processing(self):
        """API should keep large transfers pending until the 2FA challenge is confirmed."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000025",
            currency=Account.Currency.USD,
            balance=Decimal("120.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000026",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "40.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-2fa-large-1",
            format="json",
        )

        transaction = Transaction.objects.get()
        challenge = TransactionChallenge.objects.get(transaction=transaction)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(transaction.status, Transaction.Status.PENDING)
        self.assertEqual(TransactionOutbox.objects.count(), 0)
        self.assertTrue(response.data["requires_2fa"])
        self.assertEqual(response.data["challenge"]["status"], TransactionChallenge.Status.PENDING)
        self.assertTrue(response.data["challenge"]["debug_code"])

        with self.captureOnCommitCallbacks(execute=True):
            confirm_response = self.client.post(
                reverse("transaction-challenge-confirm", kwargs={"pk": transaction.id}),
                {"code": response.data["challenge"]["debug_code"]},
                format="json",
            )

        transaction.refresh_from_db()
        challenge.refresh_from_db()
        from_account.refresh_from_db()
        to_account.refresh_from_db()

        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK)
        self.assertEqual(challenge.status, TransactionChallenge.Status.VERIFIED)
        self.assertEqual(transaction.status, Transaction.Status.COMPLETED)
        self.assertEqual(TransactionOutbox.objects.count(), 1)
        self.assertEqual(from_account.balance, Decimal("80.00"))
        self.assertEqual(to_account.balance, Decimal("50.00"))

    @override_settings(
        FRAUD_FREQUENCY_WINDOW_SECONDS=60,
        FRAUD_FREQUENCY_MAX_ATTEMPTS=2,
        FRAUD_FREQUENCY_ACTION="block",
    )
    def test_transfer_returns_bad_request_when_behavioral_frequency_rule_blocks(self):
        """API should reject suspicious transaction bursts via fraud rules."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000027",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000028",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            request_id="api-fraud-prior-1",
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            request_id="api-fraud-prior-2",
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "25.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-fraud-block-1",
            HTTP_X_COUNTRY_CODE="UA",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "Transaction was blocked by behavioral fraud checks.",
        )
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(FraudEvent.objects.count(), 3)

    @override_settings(
        FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS=3,
        FRAUD_AMOUNT_ANOMALY_MULTIPLIER=Decimal("2.50"),
        FRAUD_AMOUNT_ACTION="block",
    )
    def test_transfer_returns_bad_request_when_amount_anomaly_rule_blocks(self):
        """API should reject anomalously large amounts when fraud rule is blocking."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000037",
            currency=Account.Currency.USD,
            balance=Decimal("500.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000038",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        for index, historical_amount in enumerate(("10.00", "12.00", "14.00"), start=1):
            Transaction.objects.create(
                initiated_by=self.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=f"api-amount-block-existing-{index}",
                request_fingerprint=f"api-amount-block-existing-{index}",
                amount=Decimal(historical_amount),
                status=Transaction.Status.COMPLETED,
                transfer_type=Transaction.TransferType.INTERNAL,
            )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "40.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-fraud-amount-block-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "Transaction was blocked by behavioral fraud checks.",
        )
        self.assertEqual(Transaction.objects.count(), 3)
        self.assertEqual(
            FraudEvent.objects.latest("id").outcome,
            FraudEvent.Outcome.BLOCKED,
        )

    @override_settings(
        FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS=3,
        FRAUD_AMOUNT_ANOMALY_MULTIPLIER=Decimal("2.50"),
        FRAUD_AMOUNT_ACTION="flag",
    )
    def test_transfer_persists_flagged_fraud_event_for_amount_anomaly(self):
        """API should persist a flagged fraud event for unusually large amounts."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000039",
            currency=Account.Currency.USD,
            balance=Decimal("500.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000040",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        for index, historical_amount in enumerate(("10.00", "12.00", "14.00"), start=1):
            Transaction.objects.create(
                initiated_by=self.user,
                from_account=from_account,
                to_account=to_account,
                idempotency_key=f"api-amount-flag-existing-{index}",
                request_fingerprint=f"api-amount-flag-existing-{index}",
                amount=Decimal(historical_amount),
                status=Transaction.Status.COMPLETED,
                transfer_type=Transaction.TransferType.INTERNAL,
            )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("transaction-list-create"),
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "40.00",
                },
                HTTP_IDEMPOTENCY_KEY="txn-fraud-amount-flag-1",
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        fraud_event = FraudEvent.objects.latest("id")
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.FLAGGED)
        self.assertEqual(fraud_event.transaction_id, response.data["id"])

    @override_settings(
        FRAUD_GEO_COUNTRY_CHANGE_WINDOW_SECONDS=7200,
        FRAUD_GEO_ACTION="flag",
    )
    def test_transfer_persists_flagged_fraud_event_for_country_change(self):
        """API should persist a flagged fraud event for abrupt country switches."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000029",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000030",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        FraudEvent.objects.create(
            user=self.user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
            outcome=FraudEvent.Outcome.ALLOWED,
            country_code="UA",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("transaction-list-create"),
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "25.00",
                },
                HTTP_IDEMPOTENCY_KEY="txn-fraud-geo-1",
                HTTP_X_COUNTRY_CODE="PL",
                HTTP_X_REGION="Mazowieckie",
                HTTP_X_CITY="Warsaw",
                format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        fraud_event = FraudEvent.objects.filter(
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
        ).latest("id")
        self.assertEqual(fraud_event.outcome, FraudEvent.Outcome.FLAGGED)
        self.assertEqual(fraud_event.country_code, "PL")
        self.assertEqual(fraud_event.city, "Warsaw")

    def test_cross_currency_transfer_fails_when_exchange_rate_is_missing(self):
        """Test that cross currency transfer fails when exchange rate is missing."""
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000025",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000026",
            currency=Account.Currency.EUR,
            balance=Decimal("10.00"),
        )

        response = self.client.post(
            reverse("transaction-list-create"),
            {
                "from_account_iban": from_account.iban,
                "to_account_iban": to_account.iban,
                "amount": "25.00",
            },
            HTTP_IDEMPOTENCY_KEY="txn-fx-missing-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "Exchange rate for USD->EUR is not available.",
        )
        self.assertEqual(Transaction.objects.count(), 0)

    def test_transfer_fails_for_foreign_sender_account(self):
        """Test that transfer fails for foreign sender account."""
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
        """Test that transfer requires idempotency key header."""
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

    @override_settings(TRANSACTION_SINGLE_LIMIT_AMOUNT=Decimal("9.99"))
    def test_transfer_returns_bad_request_when_single_limit_is_exceeded(self):
        """API should reject transfers above the configured single-transaction limit."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000037",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000038",
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
            HTTP_IDEMPOTENCY_KEY="txn-single-limit-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "The single transaction limit is exceeded.",
        )
        self.assertEqual(Transaction.objects.count(), 0)

    def test_transaction_list_filter_by_account(self):
        """Test that transaction list filter by account."""
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
        """Test that transaction status endpoint."""
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

    def test_transaction_status_endpoint_with_pending_2fa_challenge(self):
        """Status endpoint should return pending challenge state without crashing."""

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
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="status-view-2fa-1",
            request_fingerprint="fingerprint-status-view-2fa-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )
        TransactionChallenge.objects.create(
            user=self.user,
            transaction=transaction,
            status=TransactionChallenge.Status.PENDING,
            code_hash="hash",
            reason_codes="large_amount",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        response = self.client.get(
            reverse("transaction-status", kwargs={"pk": transaction.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], transaction.id)
        self.assertTrue(response.data["requires_2fa"])
        self.assertEqual(
            response.data["challenge"]["status"],
            TransactionChallenge.Status.PENDING,
        )

    @patch("apps.transactions.api.views.transactions.list_user_transactions")
    def test_transaction_status_endpoint_forces_primary_read(
        self,
        mock_list_user_transactions,
    ):
        """Test that transaction status endpoint forces primary read routing."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000951",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBB00000000000000000000000000952",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="status-view-primary-1",
            request_fingerprint="fingerprint-status-view-primary-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )
        mock_list_user_transactions.return_value = Transaction.objects.filter(
            id=transaction.id
        )

        response = self.client.get(
            reverse("transaction-status", kwargs={"pk": transaction.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_list_user_transactions.assert_called_once_with(
            user=self.user,
            force_primary=True,
        )

    def test_same_idempotency_key_returns_existing_transaction(self):
        """Test that same idempotency key returns existing transaction."""
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
        """Test that same idempotency key with different payload returns conflict."""
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

    def test_create_swift_transfer_returns_pending_transaction(self):
        """Test that SWIFT create stores pending metadata without dispatching the internal worker."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000901",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )

        response = self.client.post(
            reverse("transaction-swift-create"),
            {
                "from_account_iban": from_account.iban,
                "amount": "25.00",
                "swift_code": "DEUTDEFF500",
                "beneficiary_name": "Alice Example",
                "beneficiary_account_number": "123456789",
                "beneficiary_iban": "DE89370400440532013000",
                "beneficiary_bank_name": "Deutsche Bank",
                "beneficiary_bank_country": "DE",
                "beneficiary_address": "Berlin, Germany",
                "swift_reference": "invoice-42",
            },
            HTTP_IDEMPOTENCY_KEY="swift-create-1",
            format="json",
        )

        transaction = Transaction.objects.get()
        swift_details = SwiftTransferDetails.objects.get(transaction=transaction)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(transaction.status, Transaction.Status.PENDING)
        self.assertEqual(transaction.transfer_type, Transaction.TransferType.SWIFT)
        self.assertIsNone(transaction.to_account)
        self.assertEqual(transaction.fee_amount, Decimal("10.25"))
        self.assertEqual(transaction.fee_currency, Account.Currency.USD)
        self.assertEqual(TransactionOutbox.objects.count(), 0)
        self.assertEqual(swift_details.swift_code, "DEUTDEFF500")
        self.assertIsNotNone(swift_details.scheduled_processing_at)
        self.assertIsNotNone(swift_details.expected_completion_at)
        self.assertEqual(response.data["transfer_type"], Transaction.TransferType.SWIFT)
        self.assertIsNone(response.data["to_account"])
        self.assertIsNone(response.data["to_account_iban"])
        self.assertEqual(
            response.data["swift_details"]["beneficiary_bank_country"],
            "DE",
        )

    def test_same_idempotency_key_returns_existing_swift_transaction(self):
        """Test that SWIFT create respects idempotent replay semantics."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000902",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        payload = {
            "from_account_iban": from_account.iban,
            "amount": "25.00",
            "swift_code": "DEUTDEFF500",
            "beneficiary_name": "Alice Example",
            "beneficiary_account_number": "123456789",
            "beneficiary_iban": "DE89370400440532013000",
            "beneficiary_bank_name": "Deutsche Bank",
            "beneficiary_bank_country": "DE",
            "beneficiary_address": "Berlin, Germany",
            "swift_reference": "invoice-43",
        }

        first_response = self.client.post(
            reverse("transaction-swift-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="swift-replay-1",
            format="json",
        )
        second_response = self.client.post(
            reverse("transaction-swift-create"),
            payload,
            HTTP_IDEMPOTENCY_KEY="swift-replay-1",
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(SwiftTransferDetails.objects.count(), 1)
        self.assertEqual(first_response.data["id"], second_response.data["id"])

    @override_settings(TRANSACTION_DAILY_LIMIT_AMOUNT=Decimal("30.00"))
    def test_swift_transfer_returns_bad_request_when_daily_limit_is_exceeded(self):
        """API should reject SWIFT transfers that would exceed the daily amount limit."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000903",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=None,
            idempotency_key="swift-api-daily-existing-1",
            request_fingerprint="swift-api-daily-existing-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
            transfer_type=Transaction.TransferType.SWIFT,
        )

        response = self.client.post(
            reverse("transaction-swift-create"),
            {
                "from_account_iban": from_account.iban,
                "amount": "10.00",
                "swift_code": "DEUTDEFF500",
                "beneficiary_name": "Alice Example",
                "beneficiary_account_number": "123456789",
                "beneficiary_iban": "DE89370400440532013000",
                "beneficiary_bank_name": "Deutsche Bank",
                "beneficiary_bank_country": "DE",
                "beneficiary_address": "Berlin, Germany",
                "swift_reference": "invoice-limit-api-1",
            },
            HTTP_IDEMPOTENCY_KEY="swift-api-daily-limit-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["detail"],
            "The daily transaction limit is exceeded.",
        )
        self.assertEqual(Transaction.objects.count(), 1)


class TransactionSelectorReplicaRoutingTests(SimpleTestCase):
    """Test transaction selector replica routing behavior."""

    @override_settings(
        READ_REPLICA_ENABLED=True,
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
            "replica": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
        },
    )
    def test_list_user_transactions_uses_replica_when_enabled(self):
        """Test that list_user_transactions uses replica when enabled."""

        queryset = list_user_transactions(user=User(id=1))

        self.assertEqual(queryset.db, "replica")

    @override_settings(READ_REPLICA_ENABLED=False)
    def test_list_user_transactions_uses_primary_when_replica_disabled(self):
        """Test that list_user_transactions uses primary when replica is disabled."""

        queryset = list_user_transactions(user=User(id=1))

        self.assertEqual(queryset.db, "default")
