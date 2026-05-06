import logging

from celery import shared_task

from core.logging_context import reset_task_id, set_task_id
from core.structured_logging import log_event

from .services import sync_privatbank_exchange_rates

logger = logging.getLogger("apps.exchange")


@shared_task(bind=True)
def sync_privatbank_exchange_rates_task(self) -> int:
    """Synchronize privatbank exchange rates task."""
    task_token = set_task_id(self.request.id)
    try:
        log_event(
            logger,
            logging.INFO,
            "exchange_rates.task_started",
            message="Exchange rates sync task started.",
            provider="privatbank",
            task_id=self.request.id,
        )
        synced_count = sync_privatbank_exchange_rates()
        log_event(
            logger,
            logging.INFO,
            "exchange_rates.task_finished",
            message="Exchange rates sync task finished.",
            provider="privatbank",
            task_id=self.request.id,
            count=synced_count,
        )
        return synced_count
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "exchange_rates.task_failed",
            message="Exchange rates sync task failed.",
            provider="privatbank",
            task_id=self.request.id,
            failure_reason=str(exc),
        )
        raise
    finally:
        reset_task_id(task_token)
