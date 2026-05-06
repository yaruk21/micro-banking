from datetime import datetime, timezone

from django.db import migrations

from apps.transactions.partitioning import (
    add_months,
    build_transaction_partition_name,
    iter_month_starts,
    normalize_month_start,
)


# Quotes SQL identifiers safely for raw partition-management statements.
def _quote(schema_editor, name: str) -> str:
    """Return a database-safe quoted identifier."""

    return schema_editor.quote_name(name)


# Reads a single scalar value from a raw SQL query.
def _fetch_one(cursor, sql: str, params=None):
    """Execute SQL and return the first column from the first row, if present."""

    cursor.execute(sql, params or [])
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0]


# Creates one monthly child partition under the transaction parent table.
def _create_month_partition(
    *,
    cursor,
    schema_editor,
    parent_table: str,
    table_name: str,
    month_start: datetime,
) -> None:
    """Create a monthly partition for the supplied month boundary."""

    partition_name = build_transaction_partition_name(
        table_name=table_name,
        month_start=month_start,
    )
    month_end = add_months(normalize_month_start(month_start), 1)
    cursor.execute(
        f"""
        CREATE TABLE {_quote(schema_editor, partition_name)}
        PARTITION OF {_quote(schema_editor, parent_table)}
        FOR VALUES FROM (%s) TO (%s)
        """,
        [month_start, month_end],
    )


# Rebuilds the transaction table as a PostgreSQL range-partitioned parent by month.
def partition_transaction_table(apps, schema_editor):
    """Convert the transaction table into monthly PostgreSQL partitions."""

    if schema_editor.connection.vendor != "postgresql":
        return

    Transaction = apps.get_model("transactions", "Transaction")
    Account = apps.get_model("accounts", "Account")
    account_field = Transaction._meta.get_field("from_account")
    to_account_field = Transaction._meta.get_field("to_account")
    initiated_by_field = Transaction._meta.get_field("initiated_by")

    base_table = Transaction._meta.db_table
    legacy_table = f"{base_table}_legacy"
    partitioned_table = f"{base_table}_partitioned"
    default_partition = f"{base_table}_default"

    account_table = Account._meta.db_table
    user_table = initiated_by_field.remote_field.model._meta.db_table

    columns = [field.column for field in Transaction._meta.local_concrete_fields]
    quoted_columns = ", ".join(_quote(schema_editor, column) for column in columns)

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"ALTER TABLE {_quote(schema_editor, base_table)} "
            f"RENAME TO {_quote(schema_editor, legacy_table)}"
        )
        cursor.execute(
            f"""
            CREATE TABLE {_quote(schema_editor, partitioned_table)}
            (
                LIKE {_quote(schema_editor, legacy_table)}
                INCLUDING DEFAULTS
            )
            PARTITION BY RANGE ({_quote(schema_editor, 'created_at')})
            """
        )

        partitioned_sequence = _fetch_one(
            cursor,
            "SELECT pg_get_serial_sequence(%s, %s)",
            [partitioned_table, "id"],
        )
        if partitioned_sequence is None:
            partitioned_sequence = f"{partitioned_table}_id_seq"
            cursor.execute(
                f"CREATE SEQUENCE {_quote(schema_editor, partitioned_sequence)}"
            )
            cursor.execute(
                f"""
                ALTER TABLE {_quote(schema_editor, partitioned_table)}
                ALTER COLUMN {_quote(schema_editor, 'id')}
                SET DEFAULT nextval(%s::regclass)
                """,
                [partitioned_sequence],
            )
            cursor.execute(
                f"""
                ALTER SEQUENCE {_quote(schema_editor, partitioned_sequence)}
                OWNED BY {_quote(schema_editor, partitioned_table)}.{_quote(schema_editor, 'id')}
                """
            )

        min_created_at = _fetch_one(
            cursor,
            f"""
            SELECT MIN({_quote(schema_editor, 'created_at')})
            FROM {_quote(schema_editor, legacy_table)}
            """,
        )
        max_created_at = _fetch_one(
            cursor,
            f"""
            SELECT MAX({_quote(schema_editor, 'created_at')})
            FROM {_quote(schema_editor, legacy_table)}
            """,
        )

        month_starts = {
            normalize_month_start(datetime.now(timezone.utc)),
            add_months(normalize_month_start(datetime.now(timezone.utc)), 1),
        }
        if min_created_at is not None and max_created_at is not None:
            month_starts.update(iter_month_starts(min_created_at, max_created_at))

        for month_start in sorted(month_starts):
            _create_month_partition(
                cursor=cursor,
                schema_editor=schema_editor,
                parent_table=partitioned_table,
                table_name=base_table,
                month_start=month_start,
            )

        cursor.execute(
            f"""
            CREATE TABLE {_quote(schema_editor, default_partition)}
            PARTITION OF {_quote(schema_editor, partitioned_table)}
            DEFAULT
            """
        )

        cursor.execute(
            f"""
            INSERT INTO {_quote(schema_editor, partitioned_table)} ({quoted_columns})
            OVERRIDING SYSTEM VALUE
            SELECT {quoted_columns}
            FROM {_quote(schema_editor, legacy_table)}
            """
        )

        if partitioned_sequence is not None:
            cursor.execute(
                f"""
                SELECT setval(
                    %s,
                    COALESCE(
                        (SELECT MAX({_quote(schema_editor, 'id')})
                         FROM {_quote(schema_editor, partitioned_table)}),
                        1
                    ),
                    true
                )
                """,
                [partitioned_sequence],
            )

        cursor.execute(
            f"DROP TABLE {_quote(schema_editor, legacy_table)}"
        )
        cursor.execute(
            f"""
            ALTER TABLE {_quote(schema_editor, partitioned_table)}
            RENAME TO {_quote(schema_editor, base_table)}
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE {_quote(schema_editor, base_table)}
            ADD CONSTRAINT {_quote(schema_editor, 'transaction_amount_positive')}
            CHECK ({_quote(schema_editor, 'amount')} > 0)
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE {_quote(schema_editor, base_table)}
            ADD CONSTRAINT {_quote(schema_editor, 'transactions_transaction_from_account_fk')}
            FOREIGN KEY ({_quote(schema_editor, account_field.attname)})
            REFERENCES {_quote(schema_editor, account_table)} ({_quote(schema_editor, account_field.target_field.column)})
            ON DELETE RESTRICT
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE {_quote(schema_editor, base_table)}
            ADD CONSTRAINT {_quote(schema_editor, 'transactions_transaction_to_account_fk')}
            FOREIGN KEY ({_quote(schema_editor, to_account_field.attname)})
            REFERENCES {_quote(schema_editor, account_table)} ({_quote(schema_editor, to_account_field.target_field.column)})
            ON DELETE RESTRICT
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE {_quote(schema_editor, base_table)}
            ADD CONSTRAINT {_quote(schema_editor, 'transactions_transaction_initiated_by_fk')}
            FOREIGN KEY ({_quote(schema_editor, initiated_by_field.attname)})
            REFERENCES {_quote(schema_editor, user_table)} ({_quote(schema_editor, initiated_by_field.target_field.column)})
            ON DELETE RESTRICT
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'txn_id_idx')}
            ON {_quote(schema_editor, base_table)} ({_quote(schema_editor, 'id')})
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'txn_status_idx')}
            ON {_quote(schema_editor, base_table)} ({_quote(schema_editor, 'status')})
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'txn_created_at_idx')}
            ON {_quote(schema_editor, base_table)} ({_quote(schema_editor, 'created_at')})
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'transaction_from_created_idx')}
            ON {_quote(schema_editor, base_table)} (
                {_quote(schema_editor, account_field.attname)},
                {_quote(schema_editor, 'created_at')}
            )
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'transaction_to_created_idx')}
            ON {_quote(schema_editor, base_table)} (
                {_quote(schema_editor, to_account_field.attname)},
                {_quote(schema_editor, 'created_at')}
            )
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'txn_status_created_idx')}
            ON {_quote(schema_editor, base_table)} (
                {_quote(schema_editor, 'status')},
                {_quote(schema_editor, 'created_at')}
            )
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX {_quote(schema_editor, 'txn_status_proc_started_idx')}
            ON {_quote(schema_editor, base_table)} (
                {_quote(schema_editor, 'status')},
                {_quote(schema_editor, 'processing_started_at')}
            )
            """
        )


# Applies the PostgreSQL-only transaction partitioning migration step.
class Migration(migrations.Migration):
    """Swap the transaction table to a monthly range-partitioned layout."""

    dependencies = [
        ("transactions", "0009_transactionidempotencykey_and_more"),
    ]

    operations = [
        migrations.RunPython(
            partition_transaction_table,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
