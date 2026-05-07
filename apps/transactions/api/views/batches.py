from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.transactions.application import (
    BatchTransferItemInput,
    IdempotencyConflictError,
    TransactionValidationError,
    create_transaction_batch,
)
from apps.transactions.models import TransactionBatch

from ..serializers.batches import (
    TransactionBatchCreateSerializer,
    TransactionBatchReadSerializer,
)


class TransactionBatchCreateView(generics.GenericAPIView):
    """Handle transaction batch create API requests."""

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
                description=(
                    "The Idempotency-Key is already used for a different batch payload."
                )
            ),
        },
        description=(
            "Creates an asynchronous transaction batch. Validation and item "
            "processing continue in the background."
        ),
    )
    def post(self, request, *args, **kwargs):
        """Handle post."""

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ValidationError({"detail": "Idempotency-Key header is required."})

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
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=response_status)


class TransactionBatchStatusView(generics.GenericAPIView):
    """Handle transaction batch status API requests."""

    serializer_class = TransactionBatchReadSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={
            200: TransactionBatchReadSerializer,
            404: OpenApiResponse(description="Transaction batch not found."),
        },
        description=(
            "Returns the current asynchronous batch processing status for "
            "polling clients."
        ),
    )
    def get(self, request, *args, **kwargs):
        """Handle get."""

        batch = (
            TransactionBatch.objects.prefetch_related("items__transaction")
            .filter(id=kwargs["pk"], initiated_by=request.user)
            .first()
        )
        if batch is None:
            raise NotFound("Transaction batch not found.")

        serializer = self.get_serializer(batch)
        return Response(serializer.data)
