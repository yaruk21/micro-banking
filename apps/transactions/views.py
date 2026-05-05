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

from .filters import TransactionFilter
from .models import Transaction
from .selectors import list_user_transactions
from .serializers import (
    TransactionCreateSerializer,
    TransactionReadSerializer,
    TransactionStatusSerializer,
)
from .services import (
    IdempotencyConflictError,
    TransferInput,
    TransactionPermissionError,
    TransactionValidationError,
    create_transfer,
)
from .tasks import process_transfer_task


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
        cache.set(cache_key, response.data)
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
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        if created:
            process_transfer_task.delay(transaction.id)
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
