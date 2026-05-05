import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from .logging_context import (
    get_correlation_id,
    get_task_id,
    reset_correlation_id,
    reset_task_id,
    set_correlation_id,
    set_task_id,
)

STRUCTURED_LOG_FIELDS = (
    "event",
    "correlation_id",
    "task_id",
    "transaction_id",
    "transaction_ids",
    "user_id",
    "from_account_id",
    "to_account_id",
    "amount",
    "status",
    "idempotency_key",
    "request_fingerprint",
    "failure_reason",
    "count",
    "path",
    "method",
)


def normalize_log_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            key: normalize_log_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [normalize_log_value(item) for item in value]
    return value


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: Optional[str] = None,
    **fields: Any,
) -> None:
    logger.log(
        level,
        message or event,
        extra={
            "event": event,
            **fields,
        },
    )


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "correlation_id", None) is None:
            record.correlation_id = get_correlation_id()
        if getattr(record, "task_id", None) is None:
            record.task_id = get_task_id()

        for field_name in STRUCTURED_LOG_FIELDS:
            if not hasattr(record, field_name):
                setattr(record, field_name, None)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field_name in STRUCTURED_LOG_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = normalize_log_value(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


class RequestIdMiddleware:
    header_name = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = request.headers.get(self.header_name, "").strip() or str(uuid4())
        request.request_id = correlation_id
        request.correlation_id = correlation_id
        correlation_token = set_correlation_id(correlation_id)
        task_token = set_task_id(None)

        try:
            response = self.get_response(request)
        finally:
            reset_task_id(task_token)
            reset_correlation_id(correlation_token)

        response[self.header_name] = correlation_id
        return response
