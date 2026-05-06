from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.exchange.cache import get_cached_exchange_rate, set_cached_exchange_rate
from apps.exchange.models import ExchangeRate
from apps.exchange.services import (
    sync_privatbank_exchange_rates,
    upsert_exchange_rate,
)


class ExchangeRateServiceTests(TestCase):
    """Test exchange rate service test behavior."""
    def setUp(self):
        """Handle set up."""
        cache.clear()

    def test_upsert_exchange_rate_creates_new_rate(self):
        """Test that upsert exchange rate creates new rate."""
        fetched_at = timezone.now()

        exchange_rate = upsert_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at=fetched_at,
        )

        self.assertEqual(ExchangeRate.objects.count(), 1)
        self.assertEqual(exchange_rate.base_currency, "USD")
        self.assertEqual(exchange_rate.quote_currency, "UAH")
        self.assertEqual(exchange_rate.rate, Decimal("41.50000000"))
        self.assertEqual(exchange_rate.provider, "privatbank")
        self.assertEqual(exchange_rate.fetched_at, fetched_at)

    def test_upsert_exchange_rate_updates_existing_rate(self):
        """Test that upsert exchange rate updates existing rate."""
        original_fetched_at = timezone.now()
        updated_fetched_at = original_fetched_at + timezone.timedelta(minutes=15)
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at=original_fetched_at,
        )

        exchange_rate = upsert_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("42.25000000"),
            provider="privatbank",
            fetched_at=updated_fetched_at,
        )

        self.assertEqual(ExchangeRate.objects.count(), 1)
        self.assertEqual(exchange_rate.rate, Decimal("42.25000000"))
        self.assertEqual(exchange_rate.fetched_at, updated_fetched_at)

    def test_upsert_exchange_rate_invalidates_existing_cache(self):
        """Test that upsert exchange rate invalidates existing cache."""
        fetched_at = timezone.now()
        set_cached_exchange_rate(
            provider="privatbank",
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("40.00000000"),
        )

        upsert_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at=fetched_at,
        )
        self.assertIsNone(
            get_cached_exchange_rate(
                provider="privatbank",
                base_currency="USD",
                quote_currency="UAH",
            )
        )

    def test_sync_privatbank_exchange_rates_creates_rates(self):
        """Test that sync privatbank exchange rates creates rates."""
        fetched_at = datetime(2026, 5, 5, 12, 0, tzinfo=dt_timezone.utc)

        with patch("apps.exchange.services.timezone.now", return_value=fetched_at):
            with patch("apps.exchange.services.get_privatbank_rates") as mock_get_rates:
                mock_get_rates.return_value = [
                    {
                        "base_currency": "USD",
                        "quote_currency": "UAH",
                        "rate": Decimal("41.50000000"),
                        "provider": "privatbank",
                    },
                    {
                        "base_currency": "EUR",
                        "quote_currency": "UAH",
                        "rate": Decimal("44.70000000"),
                        "provider": "privatbank",
                    },
                ]

                synced_count = sync_privatbank_exchange_rates()

        self.assertEqual(synced_count, 2)
        self.assertEqual(ExchangeRate.objects.count(), 2)
        self.assertEqual(
            ExchangeRate.objects.get(base_currency="USD", quote_currency="UAH").rate,
            Decimal("41.50000000"),
        )
        self.assertEqual(
            ExchangeRate.objects.get(base_currency="EUR", quote_currency="UAH").rate,
            Decimal("44.70000000"),
        )
        self.assertEqual(
            ExchangeRate.objects.filter(fetched_at=fetched_at).count(),
            2,
        )

    def test_sync_privatbank_exchange_rates_updates_existing_rates(self):
        """Test that sync privatbank exchange rates updates existing rates."""
        original_fetched_at = datetime(2026, 5, 5, 10, 0, tzinfo=dt_timezone.utc)
        updated_fetched_at = datetime(2026, 5, 5, 13, 0, tzinfo=dt_timezone.utc)
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("40.00000000"),
            provider="privatbank",
            fetched_at=original_fetched_at,
        )

        with patch(
            "apps.exchange.services.timezone.now",
            return_value=updated_fetched_at,
        ):
            with patch(
                "apps.exchange.services.get_privatbank_rates"
            ) as mock_get_rates:
                mock_get_rates.return_value = [
                    {
                        "base_currency": "USD",
                        "quote_currency": "UAH",
                        "rate": Decimal("42.10000000"),
                        "provider": "privatbank",
                    }
                ]

                synced_count = sync_privatbank_exchange_rates()

        self.assertEqual(synced_count, 1)
        self.assertEqual(ExchangeRate.objects.count(), 1)
        exchange_rate = ExchangeRate.objects.get(
            base_currency="USD",
            quote_currency="UAH",
            provider="privatbank",
        )
        self.assertEqual(exchange_rate.rate, Decimal("42.10000000"))
        self.assertEqual(exchange_rate.fetched_at, updated_fetched_at)
