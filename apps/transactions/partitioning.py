from __future__ import annotations

from datetime import datetime, timezone

from django.db import connection


def normalize_month_start(value: datetime) -> datetime:
    """Normalize a timestamp to the first moment of its month in UTC."""

    normalized = value.astimezone(timezone.utc)
    return normalized.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def add_months(value: datetime, months: int) -> datetime:
    """Shift a month-start timestamp by a given number of calendar months."""

    month_index = (value.year * 12 + (value.month - 1)) + months
    year = month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month, day=1)


def iter_month_starts(start: datetime, end: datetime) -> list[datetime]:
    """Return all inclusive UTC month starts between two timestamps."""

    current = normalize_month_start(start)
    final = normalize_month_start(end)
    month_starts: list[datetime] = []

    while current <= final:
        month_starts.append(current)
        current = add_months(current, 1)

    return month_starts


def build_transaction_partition_name(*, table_name: str, month_start: datetime) -> str:
    """Build the PostgreSQL partition table name for a given month."""

    normalized = normalize_month_start(month_start)
    return f"{table_name}_y{normalized.year}m{normalized.month:02d}"


def build_transaction_partition_bounds(*, month_start: datetime) -> tuple[datetime, datetime]:
    """Return the inclusive lower and exclusive upper bounds for one monthly partition."""

    normalized = normalize_month_start(month_start)
    return normalized, add_months(normalized, 1)


def get_future_transaction_partition_month_starts(
    *,
    months_ahead: int,
    now: datetime | None = None,
) -> list[datetime]:
    """Return the current and future month starts that should have transaction partitions."""

    if months_ahead < 0:
        raise ValueError("months_ahead must be greater than or equal to 0.")

    reference_time = now or datetime.now(timezone.utc)
    current_month_start = normalize_month_start(reference_time)
    return [
        add_months(current_month_start, offset)
        for offset in range(months_ahead + 1)
    ]


def ensure_transaction_partitions(
    *,
    months_ahead: int = 2,
    now: datetime | None = None,
) -> int:
    """Create any missing future monthly transaction partitions in PostgreSQL."""

    if connection.vendor != "postgresql":
        return 0

    from apps.transactions.models import Transaction

    parent_table = Transaction._meta.db_table
    lock_key = f"{parent_table}:partition-maintenance"
    created_count = 0

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", [lock_key])
        try:
            for month_start in get_future_transaction_partition_month_starts(
                months_ahead=months_ahead,
                now=now,
            ):
                partition_name = build_transaction_partition_name(
                    table_name=parent_table,
                    month_start=month_start,
                )
                if _transaction_partition_exists(cursor=cursor, partition_name=partition_name):
                    continue

                range_start, range_end = build_transaction_partition_bounds(
                    month_start=month_start
                )
                cursor.execute(
                    f"""
                    CREATE TABLE {partition_name}
                    PARTITION OF {parent_table}
                    FOR VALUES FROM (%s) TO (%s)
                    """,
                    [range_start, range_end],
                )
                created_count += 1
        finally:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [lock_key])

    return created_count


def _transaction_partition_exists(*, cursor, partition_name: str) -> bool:
    """Return whether a PostgreSQL table with the partition name already exists."""

    cursor.execute("SELECT to_regclass(%s)", [partition_name])
    return cursor.fetchone()[0] is not None
