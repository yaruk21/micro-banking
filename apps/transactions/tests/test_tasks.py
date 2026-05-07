from unittest.mock import patch

from django.test import TestCase, override_settings

from core.metrics import reset_metrics
from apps.transactions.tasks import (
    dispatch_due_swift_transfers_task,
    ensure_transaction_partitions_task,
    generate_transaction_report_task,
    process_swift_transfer_task,
)


class TransactionPartitionTaskTests(TestCase):
    """Test transaction partition maintenance task behavior."""

    def setUp(self):
        reset_metrics()

    @override_settings(TRANSACTION_PARTITION_MONTHS_AHEAD=4)
    @patch("apps.transactions.workers.celery_tasks.ensure_transaction_partitions")
    def test_ensure_transaction_partitions_task_calls_service(self, mock_ensure):
        """ensure_transaction_partitions_task should call the partition maintenance service."""

        mock_ensure.return_value = 2

        created_count = ensure_transaction_partitions_task()

        self.assertEqual(created_count, 2)
        mock_ensure.assert_called_once_with(months_ahead=4)


class SwiftTransferTaskTests(TestCase):
    """Test delayed SWIFT pickup and processing task behavior."""

    def setUp(self):
        reset_metrics()

    @override_settings(SWIFT_TRANSFER_PICKUP_BATCH_SIZE=3)
    @patch("apps.transactions.workers.celery_tasks.process_swift_transfer_task.delay")
    @patch("apps.transactions.workers.celery_tasks.get_due_swift_transaction_ids")
    def test_dispatch_due_swift_transfers_task_dispatches_due_ids(
        self,
        mock_get_due_ids,
        mock_delay,
    ):
        """Dispatcher task should fan out due SWIFT ids to the dedicated worker task."""

        mock_get_due_ids.return_value = [11, 12]

        dispatched_count = dispatch_due_swift_transfers_task()

        self.assertEqual(dispatched_count, 2)
        mock_get_due_ids.assert_called_once_with(limit=3)
        self.assertEqual(mock_delay.call_count, 2)
        mock_delay.assert_any_call(11, correlation_id=None)
        mock_delay.assert_any_call(12, correlation_id=None)

    @patch("apps.transactions.workers.celery_tasks.process_swift_transfer")
    def test_process_swift_transfer_task_calls_service(self, mock_process):
        """SWIFT worker task should delegate to the SWIFT processing service."""

        mock_process.return_value = type(
            "TransferResult",
            (),
            {
                "status": "completed",
                "idempotency_key": "swift-task-1",
            },
        )()

        process_swift_transfer_task(transaction_id=17, correlation_id="corr-17")

        mock_process.assert_called_once_with(transaction_id=17)


class TransactionReportTaskTests(TestCase):
    """Test background transaction report task behavior."""

    def setUp(self):
        reset_metrics()

    @patch("apps.transactions.workers.celery_tasks.process_transaction_report")
    def test_generate_transaction_report_task_calls_service(self, mock_process):
        """Report worker task should delegate to the PDF generation service."""

        mock_process.return_value = type(
            "ReportResult",
            (),
            {
                "status": "completed",
            },
        )()

        generate_transaction_report_task(report_id=23)

        mock_process.assert_called_once_with(report_id=23)
