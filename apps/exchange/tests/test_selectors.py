from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from apps.exchange.cache import (
    get_cached_exchange_rate,
    set_cached_exchange_rate,
)
from apps.exchange.models import ExchangeRate
from apps.exchange.selectors import get_exchange_rate


class ExchangeRateSelectorTests(TestCase):
    """Test exchange rate selector test behavior."""
    def setUp(self):
        """Handle set up."""
        cache.clear()

    def test_get_exchange_rate_returns_one_for_same_currency(self):
        """Test that get exchange rate returns one for same currency."""
        rate = get_exchange_rate(
            base_currency="USD",
            quote_currency="USD",
        )

        self.assertEqual(rate, Decimal("1"))

    def test_get_exchange_rate_returns_direct_rate(self):
        """Test that get exchange rate returns direct rate."""
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )

        rate = get_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
        )

        self.assertEqual(rate, Decimal("41.50000000"))
        self.assertEqual(
            get_cached_exchange_rate(
                provider="privatbank",
                base_currency="USD",
                quote_currency="UAH",
            ),
            Decimal("41.50000000"),
        )

    def test_get_exchange_rate_returns_cross_rate_via_uah(self):
        """Test that get exchange rate returns cross rate via uah."""
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )
        ExchangeRate.objects.create(
            base_currency="EUR",
            quote_currency="UAH",
            rate=Decimal("44.70000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )

        rate = get_exchange_rate(
            base_currency="USD",
            quote_currency="EUR",
        )

        self.assertEqual(rate, Decimal("41.50000000") / Decimal("44.70000000"))
        self.assertEqual(
            get_cached_exchange_rate(
                provider="privatbank",
                base_currency="USD",
                quote_currency="EUR",
            ),
            Decimal("41.50000000") / Decimal("44.70000000"),
        )

    def test_get_exchange_rate_returns_cached_direct_rate(self):
        """Test that get exchange rate returns cached direct rate."""
        set_cached_exchange_rate(
            provider="privatbank",
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
        )

        rate = get_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
        )

        self.assertEqual(rate, Decimal("41.50000000"))

    def test_get_exchange_rate_raises_when_direct_uah_rate_is_missing(self):
        """Test that get exchange rate raises when direct uah rate is missing."""
        with self.assertRaises(ExchangeRate.DoesNotExist):
            get_exchange_rate(
                base_currency="USD",
                quote_currency="UAH",
            )

    def test_get_exchange_rate_raises_when_cross_rate_components_are_missing(self):
        """Test that get exchange rate raises when cross rate components are missing."""
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )

        with self.assertRaises(ExchangeRate.DoesNotExist):
            get_exchange_rate(
                base_currency="USD",
                quote_currency="EUR",
            )

    def test_get_exchange_rate_filters_by_provider(self):
        """Test that get exchange rate filters by provider."""
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("39.10000000"),
            provider="manual",
            fetched_at="2026-05-05T12:00:00Z",
        )
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="UAH",
            rate=Decimal("41.50000000"),
            provider="privatbank",
            fetched_at="2026-05-05T12:00:00Z",
        )

        rate = get_exchange_rate(
            base_currency="USD",
            quote_currency="UAH",
            provider="privatbank",
        )

        self.assertEqual(rate, Decimal("41.50000000"))
