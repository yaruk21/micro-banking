from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.transactions.application import (
    IdempotencyConflictError,
    TransferInput,
    create_transfer,
    confirm_transaction_challenge,
    process_transfer,
)
from apps.transactions.application.challenge import sync_transaction_challenge_state
from apps.transactions.models import Transaction, TransactionChallenge
from apps.transactions.partitioning import ensure_transaction_partitions

User = get_user_model()


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL test database")
# Exercises PostgreSQL-only behavior that SQLite cannot validate.
class TransactionPostgresIntegrationTests(TransactionTestCase):
    """Validate concurrency, indexes, and partitioning on PostgreSQL."""

    # Creates isolated users for each PostgreSQL integration scenario.
    def setUp(self):
        """Create baseline users used by PostgreSQL integration tests."""

        self.user = User.objects.create_user(
            username="pg-alice",
            password="testpass123",
        )
        self.other_user = User.objects.create_user(
            username="pg-bob",
            password="testpass123",
        )

    # Runs callbacks in parallel threads to reproduce locking and race conditions.
    def _run_concurrently(self, *callbacks):
        """Execute several callbacks concurrently and return their results."""

        barrier = Barrier(len(callbacks))
        results = [None] * len(callbacks)

        # Gives each worker its own DB connection and synchronized start line.
        def run_callback(index, callback):
            """Execute one callback in its own thread-safe database context."""

            connections.close_all()
            try:
                barrier.wait(timeout=5)
                results[index] = callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [
                executor.submit(run_callback, index, callback)
                for index, callback in enumerate(callbacks)
            ]
            for future in futures:
                future.result(timeout=10)

        close_old_connections()
        return results

    # Verifies that the transaction hot-path indexes exist in PostgreSQL.
    def test_transaction_hot_path_indexes_exist(self):
        """The expected transaction indexes should exist after migrations."""

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                Transaction._meta.db_table,
            )

        self.assertIn("transaction_from_created_idx", constraints)
        self.assertIn("transaction_to_created_idx", constraints)
        self.assertIn("txn_id_idx", constraints)
        self.assertIn("txn_status_idx", constraints)
        self.assertIn("txn_created_at_idx", constraints)
        self.assertIn("txn_status_created_idx", constraints)
        self.assertIn("txn_status_proc_started_idx", constraints)
        self.assertEqual(
            constraints["txn_id_idx"]["columns"],
            ["id"],
        )
        self.assertEqual(
            constraints["txn_status_created_idx"]["columns"],
            ["status", "created_at"],
        )
        self.assertEqual(
            constraints["txn_status_proc_started_idx"]["columns"],
            ["status", "processing_started_at"],
        )

    # Verifies that the parent transaction table is truly range-partitioned.
    def test_transaction_table_is_monthly_range_partitioned(self):
        """The transaction table should be monthly range-partitioned on created_at."""

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_partkeydef(c.oid), p.partstrat
                FROM pg_partitioned_table p
                JOIN pg_class c ON c.oid = p.partrelid
                WHERE c.relname = %s
                """,
                [Transaction._meta.db_table],
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            partkeydef, strategy = row

            cursor.execute(
                """
                SELECT c.relname
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = %s
                ORDER BY c.relname
                """,
                [Transaction._meta.db_table],
            )
            partition_names = [name for (name,) in cursor.fetchall()]

        self.assertEqual(strategy, "r")
        self.assertEqual(partkeydef, "RANGE (created_at)")
        self.assertIn("transactions_transaction_default", partition_names)
        self.assertTrue(
            any(name.startswith("transactions_transaction_y") for name in partition_names)
        )

    # Ensures the maintenance helper can pre-create missing monthly partitions.
    def test_ensure_transaction_partitions_creates_future_partition(self):
        """ensure_transaction_partitions should create a missing future monthly partition."""

        future_partition_name = "transactions_transaction_y2026m09"

        with connection.cursor() as cursor:
            cursor.execute(
                "DROP TABLE IF EXISTS transactions_transaction_y2026m09"
            )
            cursor.execute("SELECT to_regclass(%s)", [future_partition_name])
            self.assertIsNone(cursor.fetchone()[0])

        created_count = ensure_transaction_partitions(
            months_ahead=3,
            now=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        )

        self.assertGreaterEqual(created_count, 1)

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [future_partition_name])
            self.assertEqual(cursor.fetchone()[0], future_partition_name)

    # Ensures concurrent idempotent submits collapse into a single transaction.
    def test_concurrent_create_transfer_reuses_single_transaction(self):
        """Concurrent identical submissions should reuse one transaction row."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        # Submits the exact same transfer payload from two threads.
        def submit_transfer():
            """Create the same transfer payload inside one concurrent worker."""

            transfer, created = create_transfer(
                transfer_input=TransferInput(
                    user=self.user,
                    from_account_iban=from_account.iban,
                    to_account_iban=to_account.iban,
                    amount=Decimal("25.00"),
                    idempotency_key="pg-replay-1",
                )
            )
            return transfer.id, created

        results = self._run_concurrently(submit_transfer, submit_transfer)

        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(
            {transaction_id for transaction_id, _ in results},
            {Transaction.objects.get().id},
        )
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(sum(1 for _, created in results if not created), 1)

    # Ensures concurrent mismatched payloads produce one success and one conflict.
    def test_concurrent_create_transfer_with_different_payload_returns_conflict(self):
        """Concurrent requests with one reused key and different payloads should conflict."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000022",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        # Wraps transfer creation so each thread can race with a different amount.
        def submit_transfer(amount):
            """Build a concurrent worker that submits one amount variant."""

            # Performs one transfer attempt and normalizes the result for assertions.
            def callback():
                """Submit one transfer attempt inside the worker thread."""

                try:
                    transfer, created = create_transfer(
                        transfer_input=TransferInput(
                            user=self.user,
                            from_account_iban=from_account.iban,
                            to_account_iban=to_account.iban,
                            amount=amount,
                            idempotency_key="pg-replay-2",
                        )
                    )
                except IdempotencyConflictError:
                    return "conflict"
                return ("created", transfer.id, created)

            return callback

        results = self._run_concurrently(
            submit_transfer(Decimal("25.00")),
            submit_transfer(Decimal("30.00")),
        )

        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(sum(1 for result in results if result == "conflict"), 1)
        successful_results = [
            result
            for result in results
            if isinstance(result, tuple) and result[0] == "created"
        ]
        self.assertEqual(len(successful_results), 1)
        self.assertTrue(successful_results[0][2])

    # Ensures two workers cannot apply the same completed movement twice.
    def test_concurrent_process_transfer_applies_money_movement_once(self):
        """Concurrent processing of one transfer should move money only once."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000031",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000032",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        transfer, created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=to_account.iban,
                amount=Decimal("30.00"),
                idempotency_key="pg-process-1",
            )
        )

        self.assertTrue(created)

        # Processes the same transaction from two concurrent workers.
        def process_existing_transfer():
            """Run process_transfer for the same transaction from one worker."""

            processed_transfer = process_transfer(transaction_id=transfer.id)
            return processed_transfer.status

        results = self._run_concurrently(
            process_existing_transfer,
            process_existing_transfer,
        )

        from_account.refresh_from_db()
        to_account.refresh_from_db()
        transfer.refresh_from_db()

        self.assertEqual(
            results,
            [Transaction.Status.COMPLETED, Transaction.Status.COMPLETED],
        )
        self.assertEqual(transfer.status, Transaction.Status.COMPLETED)
        self.assertEqual(from_account.balance, Decimal("70.00"))
        self.assertEqual(to_account.balance, Decimal("30.00"))

    # Ensures locking prevents two concurrent transfers from overdrawing one sender.
    def test_concurrent_processing_prevents_double_spend(self):
        """Concurrent transfers from one balance should allow only one completion."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000041",
            currency=Account.Currency.USD,
            balance=Decimal("50.00"),
        )
        first_recipient = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000042",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        second_recipient = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000043",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )

        first_transfer, first_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=first_recipient.iban,
                amount=Decimal("40.00"),
                idempotency_key="pg-double-spend-1",
            )
        )
        second_transfer, second_created = create_transfer(
            transfer_input=TransferInput(
                user=self.user,
                from_account_iban=from_account.iban,
                to_account_iban=second_recipient.iban,
                amount=Decimal("40.00"),
                idempotency_key="pg-double-spend-2",
            )
        )

        self.assertTrue(first_created)
        self.assertTrue(second_created)

        results = self._run_concurrently(
            lambda: process_transfer(transaction_id=first_transfer.id).status,
            lambda: process_transfer(transaction_id=second_transfer.id).status,
        )

        from_account.refresh_from_db()
        first_recipient.refresh_from_db()
        second_recipient.refresh_from_db()
        first_transfer.refresh_from_db()
        second_transfer.refresh_from_db()

        self.assertEqual(
            sum(1 for result in results if result == Transaction.Status.COMPLETED),
            1,
        )
        self.assertEqual(
            sum(1 for result in results if result == Transaction.Status.FAILED),
            1,
        )
        self.assertEqual(from_account.balance, Decimal("10.00"))
        self.assertEqual(
            sorted([first_recipient.balance, second_recipient.balance]),
            [Decimal("0.00"), Decimal("40.00")],
        )
        self.assertEqual(
            sorted([first_transfer.status, second_transfer.status]),
            [Transaction.Status.COMPLETED, Transaction.Status.FAILED],
        )

    def test_pending_challenge_helpers_work_on_postgres_without_outer_join_locking(self):
        """Challenge status sync and confirm should work on PostgreSQL without join-lock errors."""

        from_account = Account.objects.create(
            owner=self.user,
            iban="MBP00000000000000000000000000051",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBP00000000000000000000000000052",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="pg-challenge-1",
            request_fingerprint="pg-challenge-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )
        TransactionChallenge.objects.create(
            user=self.user,
            transaction=transaction,
            status=TransactionChallenge.Status.PENDING,
            code_hash=make_password("123456"),
            reason_codes="large_amount",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        synced_transaction = sync_transaction_challenge_state(transaction=transaction)
        confirmed_transaction = confirm_transaction_challenge(
            user=self.user,
            transaction_id=transaction.id,
            code="123456",
        )

        self.assertEqual(synced_transaction.id, transaction.id)
        self.assertEqual(confirmed_transaction.id, transaction.id)
