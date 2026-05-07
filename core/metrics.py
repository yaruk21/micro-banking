import time
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
from typing import Optional

from django.conf import settings
from django.core.cache import cache


HTTP_DURATION_BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000)
TASK_DURATION_BUCKETS_MS = (100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
TRANSACTION_DURATION_BUCKETS_MS = (
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
    180000,
)

HTTP_SCOPES = ("health", "auth", "accounts", "transactions", "docs", "admin", "other")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")
TRANSFER_TYPES = ("internal", "swift")
TRANSFER_RESULTS = ("accepted", "replayed")
TRANSACTION_FINAL_STATUSES = ("completed", "failed")
REPORT_EVENTS = ("requested", "completed", "failed")
TASK_NAMES = (
    "publish_pending_transaction_outbox",
    "dispatch_due_swift_transfers",
    "process_transaction_batch",
    "process_transfer",
    "process_swift_transfer",
    "generate_transaction_report",
    "recover_stuck_transfers",
    "ensure_transaction_partitions",
)
TASK_RESULTS = ("completed", "failed")


def record_http_request(*, method: str, path: str, status_code: int, duration_seconds: float) -> None:
    """Record one HTTP request counter and latency observation."""

    scope = classify_http_scope(path=path)
    status_class = f"{status_code // 100}xx"
    increment_counter(
        name="micro_banking_http_requests_total",
        labels={
            "method": method.upper(),
            "scope": scope,
            "status_class": status_class,
        },
    )
    observe_histogram(
        name="micro_banking_http_request_duration_seconds",
        duration_seconds=duration_seconds,
        labels={"method": method.upper(), "scope": scope},
        buckets_ms=HTTP_DURATION_BUCKETS_MS,
    )


def record_transaction_request(*, transfer_type: str, outcome: str) -> None:
    """Record one accepted or replayed transaction intake result."""

    increment_counter(
        name="micro_banking_transaction_requests_total",
        labels={"transfer_type": transfer_type, "outcome": outcome},
    )


def record_transaction_result(
    *,
    transfer_type: str,
    status: str,
    amount: Decimal,
    duration_seconds: float,
) -> None:
    """Record one final transaction processing result."""

    increment_counter(
        name="micro_banking_transaction_results_total",
        labels={"transfer_type": transfer_type, "status": status},
    )
    increment_counter(
        name="micro_banking_transaction_amount_total_cents",
        labels={"transfer_type": transfer_type, "status": status},
        amount=_decimal_to_cents(amount),
    )
    observe_histogram(
        name="micro_banking_transaction_processing_duration_seconds",
        duration_seconds=duration_seconds,
        labels={"transfer_type": transfer_type, "status": status},
        buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
    )


def record_transaction_report_event(*, event: str, duration_seconds: Optional[float] = None) -> None:
    """Record one transaction report lifecycle event."""

    increment_counter(
        name="micro_banking_transaction_reports_total",
        labels={"event": event},
    )
    if duration_seconds is not None:
        observe_histogram(
            name="micro_banking_transaction_report_generation_duration_seconds",
            duration_seconds=duration_seconds,
            labels={"status": event},
            buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
        )


def record_task_result(*, task_name: str, status: str, duration_seconds: float) -> None:
    """Record one Celery task completion metric."""

    increment_counter(
        name="micro_banking_celery_tasks_total",
        labels={"task_name": task_name, "status": status},
    )
    observe_histogram(
        name="micro_banking_celery_task_duration_seconds",
        duration_seconds=duration_seconds,
        labels={"task_name": task_name},
        buckets_ms=TASK_DURATION_BUCKETS_MS,
    )


def classify_http_scope(*, path: str) -> str:
    """Map one request path to a bounded metrics scope label."""

    if path.startswith("/health/"):
        return "health"
    if path.startswith("/api/token/") or path.startswith("/api/register/"):
        return "auth"
    if path.startswith("/api/accounts/"):
        return "accounts"
    if path.startswith("/api/transactions/"):
        return "transactions"
    if path.startswith("/api/docs/") or path.startswith("/api/schema/"):
        return "docs"
    if path.startswith("/admin/"):
        return "admin"
    return "other"


def increment_counter(*, name: str, labels: dict, amount: int = 1) -> None:
    """Increment one shared counter."""

    _cache_incr(_counter_key(name=name, labels=labels), amount)


def observe_histogram(
    name: str,
    *,
    duration_seconds: float,
    labels: dict,
    buckets_ms: tuple,
) -> None:
    """Observe one duration histogram using integer millisecond buckets."""

    duration_ms = max(int(round(duration_seconds * 1000)), 0)
    _cache_incr(_hist_count_key(name=name, labels=labels), 1)
    _cache_incr(_hist_sum_key(name=name, labels=labels), duration_ms)
    for bucket in buckets_ms:
        if duration_ms <= bucket:
            _cache_incr(_hist_bucket_key(name=name, labels=labels, bucket=bucket), 1)
    _cache_incr(_hist_bucket_key(name=name, labels=labels, bucket="+Inf"), 1)


def render_metrics() -> str:
    """Render all metrics in Prometheus exposition format."""

    lines = []
    lines.extend(_render_http_metrics())
    lines.extend(_render_transaction_metrics())
    lines.extend(_render_report_metrics())
    lines.extend(_render_task_metrics())
    lines.extend(_render_live_gauges())
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """Clear all known shared metric keys for deterministic tests."""

    for key in _iter_all_metric_cache_keys():
        cache.delete(key)


def time_monotonic() -> float:
    """Return monotonic time for duration measurements."""

    return time.monotonic()


def _render_http_metrics() -> list:
    lines = [
        "# HELP micro_banking_http_requests_total Total HTTP requests by method, scope, and status class.",
        "# TYPE micro_banking_http_requests_total counter",
    ]
    for method, scope, status_class in product(HTTP_METHODS, HTTP_SCOPES, STATUS_CLASSES):
        labels = {"method": method, "scope": scope, "status_class": status_class}
        lines.append(
            _sample_line(
                "micro_banking_http_requests_total",
                labels=labels,
                value=_cache_get(_counter_key(name="micro_banking_http_requests_total", labels=labels)),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_http_request_duration_seconds HTTP request latency histogram by method and scope.",
            "# TYPE micro_banking_http_request_duration_seconds histogram",
        ]
    )
    for method, scope in product(HTTP_METHODS, HTTP_SCOPES):
        labels = {"method": method, "scope": scope}
        lines.extend(
            _histogram_lines(
                name="micro_banking_http_request_duration_seconds",
                labels=labels,
                buckets_ms=HTTP_DURATION_BUCKETS_MS,
            )
        )
    return lines


def _render_transaction_metrics() -> list:
    lines = [
        "# HELP micro_banking_transaction_requests_total Total accepted or replayed transaction intake requests.",
        "# TYPE micro_banking_transaction_requests_total counter",
    ]
    for transfer_type, outcome in product(TRANSFER_TYPES, TRANSFER_RESULTS):
        labels = {"transfer_type": transfer_type, "outcome": outcome}
        lines.append(
            _sample_line(
                "micro_banking_transaction_requests_total",
                labels=labels,
                value=_cache_get(_counter_key(name="micro_banking_transaction_requests_total", labels=labels)),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_transaction_results_total Total completed and failed transaction processing results.",
            "# TYPE micro_banking_transaction_results_total counter",
        ]
    )
    for transfer_type, status in product(TRANSFER_TYPES, TRANSACTION_FINAL_STATUSES):
        labels = {"transfer_type": transfer_type, "status": status}
        lines.append(
            _sample_line(
                "micro_banking_transaction_results_total",
                labels=labels,
                value=_cache_get(_counter_key(name="micro_banking_transaction_results_total", labels=labels)),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_transaction_amount_total Monetary volume of completed and failed transaction processing results in major currency units.",
            "# TYPE micro_banking_transaction_amount_total counter",
        ]
    )
    for transfer_type, status in product(TRANSFER_TYPES, TRANSACTION_FINAL_STATUSES):
        labels = {"transfer_type": transfer_type, "status": status}
        cents_value = _cache_get(
            _counter_key(name="micro_banking_transaction_amount_total_cents", labels=labels)
        )
        lines.append(
            _sample_line(
                "micro_banking_transaction_amount_total",
                labels=labels,
                value=_format_decimal_string(Decimal(cents_value) / Decimal("100")),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_transaction_processing_duration_seconds Transaction processing latency histogram.",
            "# TYPE micro_banking_transaction_processing_duration_seconds histogram",
        ]
    )
    for transfer_type, status in product(TRANSFER_TYPES, TRANSACTION_FINAL_STATUSES):
        labels = {"transfer_type": transfer_type, "status": status}
        lines.extend(
            _histogram_lines(
                name="micro_banking_transaction_processing_duration_seconds",
                labels=labels,
                buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
            )
        )
    return lines


def _render_report_metrics() -> list:
    lines = [
        "# HELP micro_banking_transaction_reports_total Total transaction report lifecycle events.",
        "# TYPE micro_banking_transaction_reports_total counter",
    ]
    for event in REPORT_EVENTS:
        labels = {"event": event}
        lines.append(
            _sample_line(
                "micro_banking_transaction_reports_total",
                labels=labels,
                value=_cache_get(_counter_key(name="micro_banking_transaction_reports_total", labels=labels)),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_transaction_report_generation_duration_seconds Transaction report generation latency histogram.",
            "# TYPE micro_banking_transaction_report_generation_duration_seconds histogram",
        ]
    )
    for status in ("completed", "failed"):
        labels = {"status": status}
        lines.extend(
            _histogram_lines(
                name="micro_banking_transaction_report_generation_duration_seconds",
                labels=labels,
                buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
            )
        )
    return lines


def _render_task_metrics() -> list:
    lines = [
        "# HELP micro_banking_celery_tasks_total Total Celery task completions by task name and result.",
        "# TYPE micro_banking_celery_tasks_total counter",
    ]
    for task_name, status in product(TASK_NAMES, TASK_RESULTS):
        labels = {"task_name": task_name, "status": status}
        lines.append(
            _sample_line(
                "micro_banking_celery_tasks_total",
                labels=labels,
                value=_cache_get(_counter_key(name="micro_banking_celery_tasks_total", labels=labels)),
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_celery_task_duration_seconds Celery task duration histogram.",
            "# TYPE micro_banking_celery_task_duration_seconds histogram",
        ]
    )
    for task_name in TASK_NAMES:
        labels = {"task_name": task_name}
        lines.extend(
            _histogram_lines(
                name="micro_banking_celery_task_duration_seconds",
                labels=labels,
                buckets_ms=TASK_DURATION_BUCKETS_MS,
            )
        )
    return lines


def _render_live_gauges() -> list:
    from apps.transactions.models import (
        Transaction,
        TransactionBatch,
        TransactionOutbox,
        TransactionReport,
    )

    lines = [
        "# HELP micro_banking_transaction_queue_depth Current queue depths by queue and status.",
        "# TYPE micro_banking_transaction_queue_depth gauge",
    ]
    queue_samples = [
        ("transactions", "pending", Transaction.objects.filter(status=Transaction.Status.PENDING).count()),
        ("transactions", "processing", Transaction.objects.filter(status=Transaction.Status.PROCESSING).count()),
        ("reports", "pending", TransactionReport.objects.filter(status=TransactionReport.Status.PENDING).count()),
        ("reports", "processing", TransactionReport.objects.filter(status=TransactionReport.Status.PROCESSING).count()),
        ("batches", "pending", TransactionBatch.objects.filter(status=TransactionBatch.Status.PENDING).count()),
        ("batches", "processing", TransactionBatch.objects.filter(status=TransactionBatch.Status.PROCESSING).count()),
    ]
    for queue, status, value in queue_samples:
        lines.append(
            _sample_line(
                "micro_banking_transaction_queue_depth",
                labels={"queue": queue, "status": status},
                value=value,
            )
        )
    lines.extend(
        [
            "# HELP micro_banking_transaction_outbox_pending Current pending outbox entry count.",
            "# TYPE micro_banking_transaction_outbox_pending gauge",
            _sample_line(
                "micro_banking_transaction_outbox_pending",
                labels={},
                value=TransactionOutbox.objects.filter(published_at__isnull=True).count(),
            ),
            "# HELP micro_banking_cache_backend_up Whether the configured cache backend is reachable for metrics writes.",
            "# TYPE micro_banking_cache_backend_up gauge",
            _sample_line("micro_banking_cache_backend_up", labels={}, value=_cache_backend_up()),
            "# HELP micro_banking_read_replica_enabled Whether read-replica routing is enabled.",
            "# TYPE micro_banking_read_replica_enabled gauge",
            _sample_line(
                "micro_banking_read_replica_enabled",
                labels={},
                value=1 if settings.READ_REPLICA_ENABLED else 0,
            ),
        ]
    )
    return lines


def _histogram_lines(*, name: str, labels: dict, buckets_ms: tuple) -> list:
    lines = []
    for bucket in buckets_ms:
        lines.append(
            _sample_line(
                f"{name}_bucket",
                labels={**labels, "le": _format_bucket_seconds(bucket)},
                value=_cache_get(_hist_bucket_key(name=name, labels=labels, bucket=bucket)),
            )
        )
    lines.append(
        _sample_line(
            f"{name}_bucket",
            labels={**labels, "le": "+Inf"},
            value=_cache_get(_hist_bucket_key(name=name, labels=labels, bucket="+Inf")),
        )
    )
    lines.append(
        _sample_line(
            f"{name}_count",
            labels=labels,
            value=_cache_get(_hist_count_key(name=name, labels=labels)),
        )
    )
    lines.append(
        _sample_line(
            f"{name}_sum",
            labels=labels,
            value=_format_decimal_string(
                Decimal(_cache_get(_hist_sum_key(name=name, labels=labels))) / Decimal("1000")
            ),
        )
    )
    return lines


def _sample_line(name: str, *, labels: dict, value) -> str:
    label_text = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_escape_label_value(str(value))}"'
            for key, value in sorted(labels.items())
        )
        label_text = f"{{{rendered}}}"
    return f"{name}{label_text} {value}"


def _counter_key(*, name: str, labels: dict) -> str:
    return f"metrics:counter:{name}:{_label_key(labels)}"


def _hist_bucket_key(*, name: str, labels: dict, bucket) -> str:
    return f"metrics:hist:{name}:{_label_key(labels)}:bucket:{bucket}"


def _hist_count_key(*, name: str, labels: dict) -> str:
    return f"metrics:hist:{name}:{_label_key(labels)}:count"


def _hist_sum_key(*, name: str, labels: dict) -> str:
    return f"metrics:hist:{name}:{_label_key(labels)}:sum_ms"


def _label_key(labels: dict) -> str:
    if not labels:
        return "none"
    return "|".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _cache_incr(key: str, amount: int) -> None:
    cache.add(key, 0, None)
    try:
        cache.incr(key, amount)
    except ValueError:
        cache.set(key, amount, None)


def _cache_get(key: str) -> int:
    return int(cache.get(key, 0) or 0)


def _cache_backend_up() -> int:
    probe_key = "metrics:cache_probe"
    try:
        cache.set(probe_key, 1, 5)
        return 1 if cache.get(probe_key) == 1 else 0
    except Exception:
        return 0


def _decimal_to_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _format_bucket_seconds(bucket_ms: int) -> str:
    return _format_decimal_string(Decimal(bucket_ms) / Decimal("1000"))


def _format_decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _iter_all_metric_cache_keys():
    for method, scope, status_class in product(HTTP_METHODS, HTTP_SCOPES, STATUS_CLASSES):
        labels = {"method": method, "scope": scope, "status_class": status_class}
        yield _counter_key(name="micro_banking_http_requests_total", labels=labels)
    for method, scope in product(HTTP_METHODS, HTTP_SCOPES):
        labels = {"method": method, "scope": scope}
        yield from _histogram_keys(
            name="micro_banking_http_request_duration_seconds",
            labels=labels,
            buckets_ms=HTTP_DURATION_BUCKETS_MS,
        )
    for transfer_type, outcome in product(TRANSFER_TYPES, TRANSFER_RESULTS):
        yield _counter_key(
            name="micro_banking_transaction_requests_total",
            labels={"transfer_type": transfer_type, "outcome": outcome},
        )
    for transfer_type, status in product(TRANSFER_TYPES, TRANSACTION_FINAL_STATUSES):
        labels = {"transfer_type": transfer_type, "status": status}
        yield _counter_key(name="micro_banking_transaction_results_total", labels=labels)
        yield _counter_key(name="micro_banking_transaction_amount_total_cents", labels=labels)
        yield from _histogram_keys(
            name="micro_banking_transaction_processing_duration_seconds",
            labels=labels,
            buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
        )
    for event in REPORT_EVENTS:
        yield _counter_key(
            name="micro_banking_transaction_reports_total",
            labels={"event": event},
        )
    for status in ("completed", "failed"):
        yield from _histogram_keys(
            name="micro_banking_transaction_report_generation_duration_seconds",
            labels={"status": status},
            buckets_ms=TRANSACTION_DURATION_BUCKETS_MS,
        )
    for task_name, status in product(TASK_NAMES, TASK_RESULTS):
        yield _counter_key(
            name="micro_banking_celery_tasks_total",
            labels={"task_name": task_name, "status": status},
        )
    for task_name in TASK_NAMES:
        yield from _histogram_keys(
            name="micro_banking_celery_task_duration_seconds",
            labels={"task_name": task_name},
            buckets_ms=TASK_DURATION_BUCKETS_MS,
        )


def _histogram_keys(*, name: str, labels: dict, buckets_ms: tuple):
    yield _hist_count_key(name=name, labels=labels)
    yield _hist_sum_key(name=name, labels=labels)
    for bucket in buckets_ms:
        yield _hist_bucket_key(name=name, labels=labels, bucket=bucket)
    yield _hist_bucket_key(name=name, labels=labels, bucket="+Inf")
