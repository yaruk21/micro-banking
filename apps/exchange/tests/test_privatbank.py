from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from apps.exchange.privatbank import get_privatbank_rates


class PrivatBankProviderTests(SimpleTestCase):
    @patch("apps.exchange.privatbank.requests.get")
    def test_get_privatbank_rates_returns_normalized_supported_rates(
        self,
        mock_get,
    ):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "ccy": "USD",
                "base_ccy": "UAH",
                "buy": "41.10",
                "sale": "41.90",
            },
            {
                "ccy": "EUR",
                "base_ccy": "UAH",
                "buy": "44.50",
                "sale": "45.10",
            },
            {
                "ccy": "BTC",
                "base_ccy": "USD",
                "buy": "100000.00",
                "sale": "101000.00",
            },
        ]
        mock_get.return_value = mock_response

        rates = get_privatbank_rates()

        mock_get.assert_called_once_with(
            "https://api.privatbank.ua/p24api/pubinfo?exchange&json&coursid=11",
            timeout=5,
        )
        mock_response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            rates,
            [
                {
                    "base_currency": "USD",
                    "quote_currency": "UAH",
                    "rate": Decimal("41.50"),
                    "provider": "privatbank",
                },
                {
                    "base_currency": "EUR",
                    "quote_currency": "UAH",
                    "rate": Decimal("44.80"),
                    "provider": "privatbank",
                },
            ],
        )

    @patch("apps.exchange.privatbank.requests.get")
    def test_get_privatbank_rates_ignores_invalid_or_incomplete_rows(
        self,
        mock_get,
    ):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "ccy": "USD",
                "base_ccy": "UAH",
                "buy": None,
                "sale": "41.90",
            },
            {
                "ccy": "EUR",
                "base_ccy": "UAH",
                "buy": "invalid",
                "sale": "45.10",
            },
            {
                "ccy": "USD",
                "base_ccy": "UAH",
                "buy": "-1.00",
                "sale": "41.90",
            },
            {
                "ccy": "EUR",
                "base_ccy": "UAH",
                "buy": "44.50",
                "sale": "0",
            },
            {
                "ccy": "USD",
                "base_ccy": "EUR",
                "buy": "1.00",
                "sale": "1.10",
            },
            {
                "ccy": "USD",
                "base_ccy": "UAH",
                "buy": "41.20",
                "sale": "41.80",
            },
        ]
        mock_get.return_value = mock_response

        rates = get_privatbank_rates()

        self.assertEqual(
            rates,
            [
                {
                    "base_currency": "USD",
                    "quote_currency": "UAH",
                    "rate": Decimal("41.50"),
                    "provider": "privatbank",
                }
            ],
        )

    @patch("apps.exchange.privatbank.requests.get")
    def test_get_privatbank_rates_propagates_http_errors(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("boom")
        mock_get.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            get_privatbank_rates()
