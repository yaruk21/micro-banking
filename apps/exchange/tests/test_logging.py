import json
import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.exchange.tasks import sync_privatbank_exchange_rates_task
from core.logging_context import reset_task_id, set_task_id
from core.structured_logging import JsonFormatter, RequestContextFilter, log_event


class ExchangeStructuredLoggingTests(SimpleTestCase):
    def test_json_formatter_includes_exchange_fields(self):
        logger = logging.getLogger("apps.exchange.structured-tests")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestContextFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        task_token = set_task_id("task-123")
        try:
            log_event(
                logger,
                logging.INFO,
                "exchange_rates.sync_completed",
                message="Exchange rates sync completed.",
                provider="privatbank",
                base_currency="USD",
                quote_currency="UAH",
                rate=Decimal("41.50000000"),
                fetched_at=datetime(2026, 5, 5, 12, 0, tzinfo=dt_timezone.utc),
                count=2,
            )
        finally:
            reset_task_id(task_token)
            logger.removeHandler(handler)
            handler.close()

        payload = json.loads(stream.getvalue())

        self.assertEqual(payload["event"], "exchange_rates.sync_completed")
        self.assertEqual(payload["task_id"], "task-123")
        self.assertEqual(payload["provider"], "privatbank")
        self.assertEqual(payload["base_currency"], "USD")
        self.assertEqual(payload["quote_currency"], "UAH")
        self.assertEqual(payload["rate"], "41.50000000")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["fetched_at"], "2026-05-05T12:00:00+00:00")

    @patch("apps.exchange.tasks.log_event")
    @patch("apps.exchange.tasks.sync_privatbank_exchange_rates")
    def test_exchange_sync_task_logs_start_and_finish(
        self,
        mock_sync,
        mock_log_event,
    ):
        mock_sync.return_value = 2

        result = sync_privatbank_exchange_rates_task.apply()
        synced_count = result.get()

        self.assertEqual(synced_count, 2)
        self.assertEqual(mock_log_event.call_count, 2)
        self.assertEqual(mock_log_event.call_args_list[0].args[2], "exchange_rates.task_started")
        self.assertEqual(mock_log_event.call_args_list[1].args[2], "exchange_rates.task_finished")

    @patch("apps.exchange.tasks.log_event")
    @patch("apps.exchange.tasks.sync_privatbank_exchange_rates")
    def test_exchange_sync_task_logs_failure(
        self,
        mock_sync,
        mock_log_event,
    ):
        mock_sync.side_effect = RuntimeError("provider unavailable")

        with self.assertRaises(RuntimeError):
            sync_privatbank_exchange_rates_task.apply(throw=True)

        self.assertEqual(mock_log_event.call_count, 2)
        self.assertEqual(mock_log_event.call_args_list[0].args[2], "exchange_rates.task_started")
        self.assertEqual(mock_log_event.call_args_list[1].args[2], "exchange_rates.task_failed")
