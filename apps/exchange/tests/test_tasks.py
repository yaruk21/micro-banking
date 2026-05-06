from unittest.mock import patch

from django.test import TestCase

from apps.exchange.tasks import sync_privatbank_exchange_rates_task


class ExchangeRateTaskTests(TestCase):
    """Test exchange rate task behavior."""

    @patch("apps.exchange.tasks.sync_privatbank_exchange_rates")
    def test_sync_privatbank_exchange_rates_task_calls_service(self, mock_sync):
        """Test that sync privatbank exchange rates task calls service."""
        mock_sync.return_value = 2

        synced_count = sync_privatbank_exchange_rates_task()

        self.assertEqual(synced_count, 2)
        mock_sync.assert_called_once_with()
