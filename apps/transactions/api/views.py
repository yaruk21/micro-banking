import logging

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from core.cache_utils import build_user_cache_key, get_user_cache_version
from core.structured_logging import log_event

from apps.transactions.application import (
    BatchTransferItemInput,
    IdempotencyConflictError,
    TransferInput,
    create_transaction_batch,
    TransactionPermissionError,
    TransactionValidationError,
    create_transfer,
)
from apps.transactions.models import Transaction, TransactionBatch
from apps.transactions.selectors import list_user_transactions

from .filters import TransactionFilter
from .serializers import (
    TransactionBatchCreateSerializer,
    TransactionBatchReadSerializer,
    TransactionCreateSerializer,
    TransactionReadSerializer,
    TransactionStatusSerializer,
)

logger = logging.getLogger("apps.transactions")


class TransactionListCreateView(generics.ListCreateAPIView):
    filterset_class = TransactionFilter
    ordering_fields = ("created_at", "amount")
    throttle_classes = [ScopedRateThrottle]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Transaction.objects.none()
        if not self.request.user.is_authenticated:
            return Transaction.objects.none()
        return list_user_transactions(user=self.request.user)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TransactionCreateSerializer
        return TransactionReadSerializer

    def get_throttles(self):
        self.throttle_scope = (
            "transactions_write"
            if self.request.method == "POST"
            else "transactions_read"
        )
        return super().get_throttles()

    def list(self, request, *args, **kwargs):
        version = get_user_cache_version(
            namespace="transactions_list",
            user_id=request.user.id,
        )
        cache_key = build_user_cache_key(
            namespace="transactions_list",
            user_id=request.user.id,
            version=version,
            suffix=request.get_full_path(),
        )
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        response = super().list(request, *args, **kwargs)
        cache.set(
            cache_key,
            response.data,
            timeout=settings.LIST_CACHE_TIMEOUT_SECONDS,
        )
        return response

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                location=OpenApiParameter.HEADER,
                required=True,
                type=str,
                description=(
                    "Required unique key per client-side transaction submission. "
                    "Reusing the same key with the same payload returns the "
                    "existing transaction; reusing it with a different payload "
                    "returns 409 Conflict."
                ),
            )
        ],
        responses={
            200: TransactionReadSerializer,
            202: TransactionReadSerializer,
            400: OpenApiResponse(description="Missing or invalid Idempotency-Key."),
            403: OpenApiResponse(description="You can transfer only from your own account."),
            409: OpenApiResponse(
                description="The Idempotency-Key is already used for a different payload."
            ),
        },
        description=(
            "Creates an asynchronous transfer. New requests return 202 Accepted. "
            "A retry with the same Idempotency-Key and identical payload returns "
            "the existing transaction with 200 OK."
        ),
    )
    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ValidationError(
                {"detail": "Idempotency-Key header is required."}
            )

        try:
            transaction, created = create_transfer(
                transfer_input=TransferInput(
                    user=request.user,
                    from_account_iban=serializer.validated_data["from_account"].iban,
                    to_account_iban=serializer.validated_data["to_account"].iban,
                    amount=serializer.validated_data["amount"],
                    idempotency_key=idempotency_key,
                )
            )
        except TransactionPermissionError as exc:
            raise PermissionDenied(str(exc)) from exc
        except IdempotencyConflictError as exc:
            log_event(
                logger,
                logging.WARNING,
                "transaction.response_conflict",
                message="Returning conflict response for idempotency key reuse.",
                user_id=request.user.id,
                idempotency_key=idempotency_key,
                path=request.path,
                method=request.method,
            )
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        response_serializer = TransactionReadSerializer(transaction)
        response_status = (
            status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        )
        return Response(response_serializer.data, status=response_status)


class TransactionStatusView(generics.GenericAPIView):
    serializer_class = TransactionStatusSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={200: TransactionStatusSerializer, 404: OpenApiResponse(description="Transaction not found.")},
        description="Returns the current asynchronous transaction status for polling clients.",
    )
    def get(self, request, *args, **kwargs):
        transaction = list_user_transactions(user=request.user).filter(
            id=kwargs["pk"]
        ).first()
        if transaction is None:
            raise NotFound("Transaction not found.")

        serializer = self.get_serializer(transaction)
        return Response(serializer.data)


class TransactionBatchCreateView(generics.GenericAPIView):
    serializer_class = TransactionBatchCreateSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_write"

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                location=OpenApiParameter.HEADER,
                required=True,
                type=str,
                description=(
                    "Required unique key per client-side batch submission. "
                    "Reusing the same key with the same payload returns the "
                    "existing batch; reusing it with a different payload "
                    "returns 409 Conflict."
                ),
            )
        ],
        responses={
            200: TransactionBatchReadSerializer,
            202: TransactionBatchReadSerializer,
            400: OpenApiResponse(description="Missing or invalid Idempotency-Key."),
            409: OpenApiResponse(
                description="The Idempotency-Key is already used for a different batch payload."
            ),
        },
        description=(
            "Creates an asynchronous transaction batch. Validation and item processing "
            "continue in the background."
        ),
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ValidationError(
                {"detail": "Idempotency-Key header is required."}
            )

        try:
            batch, created = create_transaction_batch(
                user=request.user,
                idempotency_key=idempotency_key,
                items=[
                    BatchTransferItemInput(**item)
                    for item in serializer.validated_data["items"]
                ],
            )
        except IdempotencyConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        response_serializer = TransactionBatchReadSerializer(batch)
        response_status = (
            status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        )
        return Response(response_serializer.data, status=response_status)


class TransactionBatchStatusView(generics.GenericAPIView):
    serializer_class = TransactionBatchReadSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={
            200: TransactionBatchReadSerializer,
            404: OpenApiResponse(description="Transaction batch not found."),
        },
        description="Returns the current asynchronous batch processing status for polling clients.",
    )
    def get(self, request, *args, **kwargs):
        batch = (
            TransactionBatch.objects.prefetch_related("items__transaction")
            .filter(id=kwargs["pk"], initiated_by=request.user)
            .first()
        )
        if batch is None:
            raise NotFound("Transaction batch not found.")

        serializer = self.get_serializer(batch)
        return Response(serializer.data)
