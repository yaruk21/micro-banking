from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from core.metrics import reset_metrics

User = get_user_model()


class MetricsEndpointTests(APITestCase):
    """Verify Prometheus-style metrics exposition."""

    def setUp(self):
        cache.clear()
        reset_metrics()

    def test_metrics_endpoint_exposes_http_counters(self):
        """Health requests should appear in the metrics exposition."""

        self.client.get(reverse("health-check"))
        response = self.client.get(reverse("metrics"))
        payload = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "text/plain; version=0.0.4; charset=utf-8",
        )
        self.assertIn("# HELP micro_banking_http_requests_total", payload)
        self.assertIn(
            'micro_banking_http_requests_total{method="GET",scope="health",status_class="2xx"} 1',
            payload,
        )


class TransactionMetricsTests(APITestCase):
    """Verify business metrics for transaction flows."""

    def setUp(self):
        cache.clear()
        reset_metrics()
        self.user = User.objects.create_user(username="metrics-alice", password="pass123")
        self.other_user = User.objects.create_user(username="metrics-bob", password="pass123")
        self.client.force_authenticate(user=self.user)

    def test_transfer_and_report_metrics_are_exposed(self):
        """Transfer acceptance and report request counters should be exported."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBM00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBM00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("transaction-list-create"),
                {
                    "from_account_iban": from_account.iban,
                    "to_account_iban": to_account.iban,
                    "amount": "10.00",
                },
                HTTP_IDEMPOTENCY_KEY="metrics-transfer-1",
                format="json",
            )
            self.client.post(
                reverse("transaction-report-create"),
                {
                    "date_from": "2026-05-01",
                    "date_to": "2026-05-07",
                },
                format="json",
            )

        response = self.client.get(reverse("metrics"))
        payload = response.content.decode()

        self.assertIn(
            'micro_banking_transaction_requests_total{outcome="accepted",transfer_type="internal"} 1',
            payload,
        )
        self.assertIn(
            'micro_banking_transaction_reports_total{event="requested"} 1',
            payload,
        )
