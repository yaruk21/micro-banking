from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.transactions.tasks import ensure_transaction_partitions_task


class TransactionPartitionTaskTests(TestCase):
    """Test transaction partition maintenance task behavior."""

    @override_settings(TRANSACTION_PARTITION_MONTHS_AHEAD=4)
    @patch("apps.transactions.workers.celery_tasks.ensure_transaction_partitions")
    def test_ensure_transaction_partitions_task_calls_service(self, mock_ensure):
        """ensure_transaction_partitions_task should call the partition maintenance service."""

        mock_ensure.return_value = 2

        created_count = ensure_transaction_partitions_task()

        self.assertEqual(created_count, 2)
        mock_ensure.assert_called_once_with(months_ahead=4)
