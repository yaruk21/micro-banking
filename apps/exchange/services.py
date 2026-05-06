import logging

from django.db import transaction
from django.utils import timezone

from .cache import invalidate_exchange_rate_cache
from .models import ExchangeRate
from .privatbank import get_privatbank_rates
from core.structured_logging import log_event

logger = logging.getLogger("apps.exchange")


def upsert_exchange_rate(*, base_currency: str, quote_currency: str, rate, provider: str, fetched_at):
    """Upsert exchange rate."""
    exchange_rate, _ = ExchangeRate.objects.update_or_create(
        base_currency=base_currency,
        quote_currency=quote_currency,
        provider=provider,
        defaults={
            "rate": rate,
            "fetched_at": fetched_at,
        },
    )
    invalidate_exchange_rate_cache(
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    log_event(
        logger,
        logging.INFO,
        "exchange_rates.rate_upserted",
        message="Exchange rate stored or updated.",
        provider=provider,
        base_currency=base_currency,
        quote_currency=quote_currency,
        rate=rate,
        fetched_at=fetched_at,
    )
    return exchange_rate


def sync_privatbank_exchange_rates() -> int:
    """Synchronize privatbank exchange rates."""
    fetched_at = timezone.now()
    log_event(
        logger,
        logging.INFO,
        "exchange_rates.sync_started",
        message="Exchange rates sync started.",
        provider="privatbank",
        fetched_at=fetched_at,
    )
    try:
        rates = get_privatbank_rates()
        synced_count = 0

        with transaction.atomic():
            for rate_data in rates:
                upsert_exchange_rate(
                    base_currency=rate_data["base_currency"],
                    quote_currency=rate_data["quote_currency"],
                    rate=rate_data["rate"],
                    provider=rate_data["provider"],
                    fetched_at=fetched_at,
                )
                synced_count += 1

        log_event(
            logger,
            logging.INFO,
            "exchange_rates.sync_completed",
            message="Exchange rates sync completed.",
            provider="privatbank",
            fetched_at=fetched_at,
            count=synced_count,
        )
        return synced_count
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "exchange_rates.sync_failed",
            message="Exchange rates sync failed.",
            provider="privatbank",
            fetched_at=fetched_at,
            failure_reason=str(exc),
        )
        raise
