import asyncio
import json
import re
from typing import Optional

import redis.asyncio as redis_asyncio
from asgiref.sync import sync_to_async
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.transactions.models import TransactionBatch
from apps.transactions.realtime import (
    build_transaction_batch_status_channel,
    build_transaction_status_channel,
    get_realtime_auth_token_from_scope,
    serialize_transaction_batch_status,
    serialize_transaction_status,
)
from apps.transactions.selectors import list_user_transactions

TRANSACTION_STATUS_PATH_RE = re.compile(r"^/ws/transactions/(?P<pk>\d+)/$")
TRANSACTION_BATCH_STATUS_PATH_RE = re.compile(
    r"^/ws/transaction-batches/(?P<pk>\d+)/$"
)


async def websocket_status_application(scope, receive, send):
    """Handle websocket status application."""
    if scope["type"] != "websocket":
        await send({"type": "websocket.close", "code": 1003})
        return

    path = scope.get("path", "")
    transaction_match = TRANSACTION_STATUS_PATH_RE.match(path)
    batch_match = TRANSACTION_BATCH_STATUS_PATH_RE.match(path)

    if transaction_match:
        transaction_id = int(transaction_match.group("pk"))
        await _serve_transaction_status_websocket(
            scope=scope,
            receive=receive,
            send=send,
            transaction_id=transaction_id,
        )
        return

    if batch_match:
        batch_id = int(batch_match.group("pk"))
        await _serve_transaction_batch_status_websocket(
            scope=scope,
            receive=receive,
            send=send,
            batch_id=batch_id,
        )
        return

    await send({"type": "websocket.close", "code": 1008})


async def _serve_transaction_status_websocket(scope, receive, send, transaction_id: int):
    """Handle serve transaction status websocket."""
    user = await _authenticate_websocket_user(scope)
    if user is None:
        await send({"type": "websocket.close", "code": 4401})
        return

    transaction = await sync_to_async(_get_user_transaction)(user.id, transaction_id)
    if transaction is None:
        await send({"type": "websocket.close", "code": 4404})
        return

    await send({"type": "websocket.accept"})
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(
                serialize_transaction_status(transaction=transaction),
                ensure_ascii=True,
            ),
        }
    )
    await _stream_channel_messages(
        receive=receive,
        send=send,
        channel=build_transaction_status_channel(transaction_id=transaction_id),
    )


async def _serve_transaction_batch_status_websocket(scope, receive, send, batch_id: int):
    """Handle serve transaction batch status websocket."""
    user = await _authenticate_websocket_user(scope)
    if user is None:
        await send({"type": "websocket.close", "code": 4401})
        return

    batch = await sync_to_async(_get_user_transaction_batch)(user.id, batch_id)
    if batch is None:
        await send({"type": "websocket.close", "code": 4404})
        return

    await send({"type": "websocket.accept"})
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(
                serialize_transaction_batch_status(batch=batch),
                ensure_ascii=True,
            ),
        }
    )
    await _stream_channel_messages(
        receive=receive,
        send=send,
        channel=build_transaction_batch_status_channel(batch_id=batch_id),
    )


async def _stream_channel_messages(*, receive, send, channel: str) -> None:
    """Handle stream channel messages."""
    client = redis_asyncio.from_url(settings.REALTIME_REDIS_URL)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)

    forward_task = asyncio.create_task(_forward_pubsub_messages(pubsub=pubsub, send=send))
    disconnect_task = asyncio.create_task(_wait_for_disconnect(receive=receive))

    try:
        done, pending = await asyncio.wait(
            {forward_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await client.aclose()


async def _forward_pubsub_messages(*, pubsub, send) -> None:
    """Handle forward pubsub messages."""
    while True:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=1.0,
        )
        if message and message.get("type") == "message":
            payload = message["data"]
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            await send(
                {
                    "type": "websocket.send",
                    "text": payload,
                }
            )
        await asyncio.sleep(0.05)


async def _wait_for_disconnect(*, receive) -> None:
    """Handle wait for disconnect."""
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return


async def _authenticate_websocket_user(scope):
    """Handle authenticate websocket user."""
    token = get_realtime_auth_token_from_scope(scope)
    if not token:
        return None
    return await sync_to_async(_get_user_from_token)(token)


def _get_user_from_token(token: str):
    """Handle get user from token."""
    authentication = JWTAuthentication()
    validated_token = authentication.get_validated_token(token)
    return authentication.get_user(validated_token)


def _get_user_transaction(user_id: int, transaction_id: int):
    """Handle get user transaction."""
    return (
        list_user_transactions(user=_get_user_model_instance(user_id))
        .filter(id=transaction_id)
        .first()
    )


def _get_user_transaction_batch(user_id: int, batch_id: int):
    """Handle get user transaction batch."""
    return (
        TransactionBatch.objects.prefetch_related("items__transaction")
        .filter(id=batch_id, initiated_by_id=user_id)
        .first()
    )


def _get_user_model_instance(user_id: int):
    """Handle get user model instance."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(id=user_id)
