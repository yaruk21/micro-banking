import json
import logging
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.transactions.models import Transaction
from core.logging_context import reset_correlation_id, set_correlation_id
from core.structured_logging import JsonFormatter, RequestContextFilter, log_event

User = get_user_model()


class StructuredLoggingTests(SimpleTestCase):
    def test_json_formatter_includes_structured_fields(self):
        logger = logging.getLogger("apps.transactions.structured-tests")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestContextFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        correlation_token = set_correlation_id("corr-123")
        try:
            log_event(
                logger,
                logging.INFO,
                "transaction.accepted",
                message="Transaction accepted for asynchronous processing.",
                transaction_id=7,
                amount=Decimal("12.34"),
                status="pending",
            )
        finally:
            reset_correlation_id(correlation_token)
            logger.removeHandler(handler)
            handler.close()

        payload = json.loads(stream.getvalue())

        self.assertEqual(payload["event"], "transaction.accepted")
        self.assertEqual(payload["correlation_id"], "corr-123")
        self.assertEqual(payload["transaction_id"], 7)
        self.assertEqual(payload["amount"], "12.34")
        self.assertEqual(payload["status"], "pending")


class RequestIdMiddlewareTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="logging-alice",
            password="testpass123",
        )
        self.other_user = User.objects.create_user(
            username="logging-bob",
            password="testpass123",
        )
        self.client.force_authenticate(user=self.user)

    @patch("apps.transactions.api.views.process_transfer_task.delay")
    def test_transaction_create_returns_request_id_and_propagates_it_to_worker(
        self,
        mock_delay,
    ):
        mock_delay.return_value = SimpleNamespace(id="celery-task-123")
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBL00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBL00000000000000000000000000012",
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
            HTTP_IDEMPOTENCY_KEY="logging-request-id-1",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("X-Request-ID", response)
        self.assertTrue(response["X-Request-ID"])

        transaction = Transaction.objects.get(id=response.data["id"])
        mock_delay.assert_called_once_with(
            transaction.id,
            correlation_id=response["X-Request-ID"],
        )
