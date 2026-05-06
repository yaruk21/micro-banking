from datetime import datetime, timezone

from django.test import SimpleTestCase

from apps.transactions.partitioning import (
    add_months,
    build_transaction_partition_bounds,
    build_transaction_partition_name,
    get_future_transaction_partition_month_starts,
    iter_month_starts,
    normalize_month_start,
)


# Covers the pure helper logic that drives monthly partition naming and ranges.
class TransactionPartitioningHelperTests(SimpleTestCase):
    """Validate the date helpers used by transaction partition management."""

    # Ensures month normalization always snaps to the UTC partition boundary.
    def test_normalize_month_start_rounds_down_to_utc_month_boundary(self):
        """normalize_month_start should snap timestamps to the first UTC month instant."""

        value = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        self.assertEqual(
            normalize_month_start(value),
            datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        )

    # Ensures helper math handles a year rollover correctly.
    def test_add_months_rolls_year_boundaries(self):
        """add_months should correctly cross calendar years."""

        value = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            add_months(value, 2),
            datetime(2027, 2, 1, 0, 0, tzinfo=timezone.utc),
        )

    # Ensures partition generation includes every month in the covered range.
    def test_iter_month_starts_returns_inclusive_month_range(self):
        """iter_month_starts should return every inclusive month start."""

        self.assertEqual(
            iter_month_starts(
                datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            ),
            [
                datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
            ],
        )

    # Ensures partition names follow the operational naming convention.
    def test_build_transaction_partition_name_uses_year_month_suffix(self):
        """build_transaction_partition_name should produce the expected suffix format."""

        self.assertEqual(
            build_transaction_partition_name(
                table_name="transactions_transaction",
                month_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            ),
            "transactions_transaction_y2026m05",
        )

    # Ensures partition bounds cover exactly one whole calendar month.
    def test_build_transaction_partition_bounds_returns_month_window(self):
        """build_transaction_partition_bounds should return one monthly UTC range."""

        self.assertEqual(
            build_transaction_partition_bounds(
                month_start=datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc),
            ),
            (
                datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            ),
        )

    # Ensures the maintenance job can pre-create multiple future partition months.
    def test_get_future_transaction_partition_month_starts_includes_current_month(self):
        """get_future_transaction_partition_month_starts should include current and future months."""

        self.assertEqual(
            get_future_transaction_partition_month_starts(
                months_ahead=2,
                now=datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc),
            ),
            [
                datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
            ],
        )
