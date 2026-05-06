import asyncio
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import Account
from apps.transactions.models import Transaction, TransactionBatch
from apps.transactions.realtime import (
    publish_transaction_batch_status_update,
    publish_transaction_status_update,
)
from core.asgi import application

User = get_user_model()


class FakeAsyncPubSub:
    """Represent fake async pub sub."""
    def __init__(self):
        """Handle init."""
        self.messages = asyncio.Queue()

    async def subscribe(self, channel):
        """Handle subscribe."""
        self.channel = channel

    async def unsubscribe(self, channel):
        """Handle unsubscribe."""
        return None

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        """Return message."""
        try:
            return self.messages.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0)
            return None

    async def close(self):
        """Handle close."""
        return None


class FakeAsyncRedisClient:
    """Represent fake async redis client."""
    def __init__(self, pubsub):
        """Handle init."""
        self._pubsub = pubsub

    def pubsub(self):
        """Handle pubsub."""
        return self._pubsub

    async def aclose(self):
        """Handle aclose."""
        return None


class TransactionWebSocketTests(TestCase):
    """Test transaction web socket test behavior."""
    def setUp(self):
        """Handle set up."""
        self.user = User.objects.create_user(
            username="ws-alice",
            password="pass123",
        )
        self.other_user = User.objects.create_user(
            username="ws-bob",
            password="pass123",
        )

    @patch("apps.transactions.realtime.redis.Redis.from_url")
    def test_publish_transaction_status_update_publishes_json_payload(self, mock_from_url):
        """Test that publish transaction status update publishes json payload."""
        client = MagicMock()
        mock_from_url.return_value = client
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBW00000000000000000000000000011",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBW00000000000000000000000000012",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="ws-status-1",
            request_fingerprint="ws-fingerprint-1",
            amount=Decimal("10.00"),
            status=Transaction.Status.PENDING,
        )

        publish_transaction_status_update(transaction_id=transaction.id)

        client.publish.assert_called_once()
        channel_name, payload = client.publish.call_args.args
        self.assertIn(str(transaction.id), channel_name)
        message = json.loads(payload)
        self.assertEqual(message["type"], "transaction.status")
        self.assertEqual(message["data"]["id"], transaction.id)

    def test_transaction_websocket_sends_snapshot_and_realtime_update(self):
        """Test that transaction websocket sends snapshot and realtime update."""
        from_account = Account.objects.create(
            owner=self.user,
            iban="MBW00000000000000000000000000021",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        to_account = Account.objects.create(
            owner=self.other_user,
            iban="MBW00000000000000000000000000022",
            currency=Account.Currency.USD,
            balance=Decimal("0.00"),
        )
        transaction = Transaction.objects.create(
            initiated_by=self.user,
            from_account=from_account,
            to_account=to_account,
            idempotency_key="ws-status-2",
            request_fingerprint="ws-fingerprint-2",
            amount=Decimal("10.00"),
            status=Transaction.Status.PENDING,
        )
        token = str(AccessToken.for_user(self.user))

        async def scenario():
            """Handle scenario."""
            pubsub = FakeAsyncPubSub()
            client = FakeAsyncRedisClient(pubsub)

            with patch("core.websocket_status.redis_asyncio.from_url", return_value=client):
                communicator = ApplicationCommunicator(
                    application,
                    {
                        "type": "websocket",
                        "path": f"/ws/transactions/{transaction.id}/",
                        "query_string": f"token={token}".encode("utf-8"),
                        "headers": [],
                    },
                )
                await communicator.send_input({"type": "websocket.connect"})

                accepted = await communicator.receive_output()
                self.assertEqual(accepted["type"], "websocket.accept")

                initial_message = await communicator.receive_output()
                initial_payload = json.loads(initial_message["text"])
                self.assertEqual(initial_payload["type"], "transaction.status")
                self.assertEqual(initial_payload["data"]["status"], Transaction.Status.PENDING)

                await pubsub.messages.put(
                    {
                        "type": "message",
                        "data": json.dumps(
                            {
                                "type": "transaction.status",
                                "data": {
                                    "id": transaction.id,
                                    "status": Transaction.Status.COMPLETED,
                                },
                            }
                        ).encode("utf-8"),
                    }
                )

                update_message = await communicator.receive_output()
                update_payload = json.loads(update_message["text"])
                self.assertEqual(update_payload["data"]["status"], Transaction.Status.COMPLETED)

                await communicator.send_input({"type": "websocket.disconnect"})
                await communicator.wait()

        async_to_sync(scenario)()

    def test_transaction_batch_websocket_sends_snapshot(self):
        """Test that transaction batch websocket sends snapshot."""
        batch = TransactionBatch.objects.create(
            initiated_by=self.user,
            idempotency_key="ws-batch-1",
            request_fingerprint="ws-batch-fingerprint-1",
            status=TransactionBatch.Status.PENDING,
            total_items=2,
        )
        token = str(AccessToken.for_user(self.user))

        async def scenario():
            """Handle scenario."""
            pubsub = FakeAsyncPubSub()
            client = FakeAsyncRedisClient(pubsub)

            with patch("core.websocket_status.redis_asyncio.from_url", return_value=client):
                communicator = ApplicationCommunicator(
                    application,
                    {
                        "type": "websocket",
                        "path": f"/ws/transaction-batches/{batch.id}/",
                        "query_string": f"token={token}".encode("utf-8"),
                        "headers": [],
                    },
                )
                await communicator.send_input({"type": "websocket.connect"})

                accepted = await communicator.receive_output()
                self.assertEqual(accepted["type"], "websocket.accept")

                initial_message = await communicator.receive_output()
                initial_payload = json.loads(initial_message["text"])
                self.assertEqual(initial_payload["type"], "transaction_batch.status")
                self.assertEqual(initial_payload["data"]["status"], TransactionBatch.Status.PENDING)

                await communicator.send_input({"type": "websocket.disconnect"})
                await communicator.wait()

        async_to_sync(scenario)()

    @patch("apps.transactions.realtime.redis.Redis.from_url")
    def test_publish_transaction_batch_status_update_publishes_json_payload(self, mock_from_url):
        """Test that publish transaction batch status update publishes json payload."""
        client = MagicMock()
        mock_from_url.return_value = client
        batch = TransactionBatch.objects.create(
            initiated_by=self.user,
            idempotency_key="ws-batch-2",
            request_fingerprint="ws-batch-fingerprint-2",
            status=TransactionBatch.Status.PENDING,
            total_items=1,
        )

        publish_transaction_batch_status_update(batch_id=batch.id)

        client.publish.assert_called_once()
        channel_name, payload = client.publish.call_args.args
        self.assertIn(str(batch.id), channel_name)
        message = json.loads(payload)
        self.assertEqual(message["type"], "transaction_batch.status")
        self.assertEqual(message["data"]["id"], batch.id)
