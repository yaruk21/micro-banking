from decimal import Decimal, InvalidOperation

import requests

PRIVATBANK_EXCHANGE_URL = (
    "https://api.privatbank.ua/p24api/pubinfo?exchange&json&coursid=11"
)

SUPPORTED_CURRENCIES = {"USD", "EUR"}
QUOTE_CURRENCY = "UAH"


def get_privatbank_rates() -> list[dict]:
    """Return privatbank rates."""
    response = requests.get(PRIVATBANK_EXCHANGE_URL, timeout=5)
    response.raise_for_status()

    payload = response.json()
    normalized_rates = []

    for item in payload:
        base_currency = item.get("ccy")
        quote_currency = item.get("base_ccy")

        if base_currency not in SUPPORTED_CURRENCIES:
            continue

        if quote_currency != QUOTE_CURRENCY:
            continue

        buy_raw = item.get("buy")
        sale_raw = item.get("sale")
        if buy_raw is None or sale_raw is None:
            continue

        try:
            buy_rate = Decimal(str(buy_raw))
            sale_rate = Decimal(str(sale_raw))
        except (InvalidOperation, TypeError):
            continue

        if buy_rate <= 0 or sale_rate <= 0:
            continue

        rate = (buy_rate + sale_rate) / Decimal("2")

        normalized_rates.append(
            {
                "base_currency": base_currency,
                "quote_currency": quote_currency,
                "rate": rate,
                "provider": "privatbank",
            }
        )

    return normalized_rates
