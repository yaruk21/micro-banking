import json
import logging
from typing import Optional

import redis
from django.conf import settings

from apps.transactions.api.serializers import (
    TransactionBatchReadSerializer,
    TransactionStatusSerializer,
)
from apps.transactions.models import Transaction, TransactionBatch
from core.structured_logging import log_event

TRANSACTION_STATUS_CHANNEL_PREFIX = "transactions.status"
TRANSACTION_BATCH_STATUS_CHANNEL_PREFIX = "transaction_batches.status"
logger = logging.getLogger("apps.transactions")


def build_transaction_status_channel(*, transaction_id: int) -> str:
    """Build transaction status channel."""
    return f"{TRANSACTION_STATUS_CHANNEL_PREFIX}.{transaction_id}"


def build_transaction_batch_status_channel(*, batch_id: int) -> str:
    """Build transaction batch status channel."""
    return f"{TRANSACTION_BATCH_STATUS_CHANNEL_PREFIX}.{batch_id}"


def serialize_transaction_status(*, transaction: Transaction) -> dict:
    """Handle serialize transaction status."""
    return {
        "type": "transaction.status",
        "data": TransactionStatusSerializer(transaction).data,
    }


def serialize_transaction_batch_status(*, batch: TransactionBatch) -> dict:
    """Handle serialize transaction batch status."""
    return {
        "type": "transaction_batch.status",
        "data": TransactionBatchReadSerializer(batch).data,
    }


def publish_transaction_status_update(*, transaction_id: int) -> None:
    """Publish transaction status update."""
    transaction = (
        Transaction.objects.select_related(
            "from_account",
            "to_account",
            "swift_details",
            "challenge",
        )
        .filter(id=transaction_id)
        .first()
    )
    if transaction is None:
        return

    _publish_json_message(
        channel=build_transaction_status_channel(transaction_id=transaction_id),
        payload=serialize_transaction_status(transaction=transaction),
    )


def publish_transaction_batch_status_update(*, batch_id: int) -> None:
    """Publish transaction batch status update."""
    batch = (
        TransactionBatch.objects.prefetch_related("items__transaction")
        .filter(id=batch_id)
        .first()
    )
    if batch is None:
        return

    _publish_json_message(
        channel=build_transaction_batch_status_channel(batch_id=batch_id),
        payload=serialize_transaction_batch_status(batch=batch),
    )


def _publish_json_message(*, channel: str, payload: dict) -> None:
    """Handle publish json message."""
    client = redis.Redis.from_url(settings.REALTIME_REDIS_URL)
    try:
        client.publish(channel, json.dumps(payload, ensure_ascii=True))
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "transaction.realtime_publish_failed",
            message="Failed to publish realtime status update.",
            failure_reason=str(exc),
        )
    finally:
        client.close()


def get_realtime_auth_token_from_scope(scope) -> Optional[str]:
    """Return realtime auth token from scope."""
    query_string = scope.get("query_string", b"").decode("utf-8")
    params = {}
    for chunk in query_string.split("&"):
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        params[key] = value

    token = params.get("token", "").strip()
    if token:
        return token

    for header_name, header_value in scope.get("headers", []):
        if header_name == b"authorization":
            value = header_value.decode("utf-8")
            if value.lower().startswith("bearer "):
                return value[7:].strip()
    return None
