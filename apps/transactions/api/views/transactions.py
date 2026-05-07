import logging

from django.core.cache import cache
from django.conf import settings
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
    IdempotencyConflictError,
    SwiftTransferInput,
    TransferInput,
    TransactionPermissionError,
    TransactionValidationError,
    confirm_transaction_challenge,
    create_swift_transfer,
    create_transfer,
)
from apps.transactions.application.challenge import (
    sync_transaction_challenge_state,
)
from apps.transactions.models import Transaction
from apps.transactions.selectors import list_user_transactions

from ..filters import TransactionFilter
from ..serializers.transactions import (
    SwiftTransactionCreateSerializer,
    TransactionChallengeConfirmSerializer,
    TransactionCreateSerializer,
    TransactionReadSerializer,
    TransactionStatusSerializer,
)
from .common import (
    build_request_fraud_context,
    build_transaction_serializer_context,
)

logger = logging.getLogger("apps.transactions")


class TransactionListCreateView(generics.ListCreateAPIView):
    """Handle transaction list create API requests."""

    filterset_class = TransactionFilter
    ordering_fields = ("created_at", "amount")
    throttle_classes = [ScopedRateThrottle]

    def get_queryset(self):
        """Return queryset."""

        if getattr(self, "swagger_fake_view", False):
            return Transaction.objects.none()
        if not self.request.user.is_authenticated:
            return Transaction.objects.none()
        return list_user_transactions(user=self.request.user)

    def get_serializer_class(self):
        """Return serializer class."""

        if self.request.method == "POST":
            return TransactionCreateSerializer
        return TransactionReadSerializer

    def get_throttles(self):
        """Return throttles."""

        self.throttle_scope = (
            "transactions_write"
            if self.request.method == "POST"
            else "transactions_read"
        )
        return super().get_throttles()

    def list(self, request, *args, **kwargs):
        """Handle list."""

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
            403: OpenApiResponse(
                description="You can transfer only from your own account."
            ),
            409: OpenApiResponse(
                description=(
                    "The Idempotency-Key is already used for a different payload."
                )
            ),
        },
        description=(
            "Creates an asynchronous transfer. New requests return 202 Accepted. "
            "A retry with the same Idempotency-Key and identical payload returns "
            "the existing transaction with 200 OK."
        ),
    )
    def post(self, request, *args, **kwargs):
        """Handle post."""

        return self.create(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """Handle create."""

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ValidationError({"detail": "Idempotency-Key header is required."})

        try:
            transaction, created = create_transfer(
                transfer_input=TransferInput(
                    user=request.user,
                    from_account_iban=serializer.validated_data["from_account"].iban,
                    to_account_iban=serializer.validated_data["to_account"].iban,
                    amount=serializer.validated_data["amount"],
                    idempotency_key=idempotency_key,
                    fraud_context=build_request_fraud_context(request),
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

        response_serializer = TransactionReadSerializer(
            transaction,
            context=build_transaction_serializer_context(
                request,
                transaction=transaction,
            ),
        )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=response_status)


class TransactionStatusView(generics.GenericAPIView):
    """Handle transaction status API requests."""

    serializer_class = TransactionStatusSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={
            200: TransactionStatusSerializer,
            404: OpenApiResponse(description="Transaction not found."),
        },
        description=(
            "Returns the current asynchronous transaction status for polling clients."
        ),
    )
    def get(self, request, *args, **kwargs):
        """Handle get."""

        transaction = list_user_transactions(
            user=request.user,
            force_primary=True,
        ).filter(id=kwargs["pk"]).first()
        if transaction is None:
            raise NotFound("Transaction not found.")
        transaction = sync_transaction_challenge_state(transaction=transaction)

        serializer = self.get_serializer(
            transaction,
            context={"request": request},
        )
        return Response(serializer.data)


class TransactionSwiftCreateView(generics.GenericAPIView):
    """Handle SWIFT transaction create API requests."""

    serializer_class = SwiftTransactionCreateSerializer
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
                    "Required unique key per client-side SWIFT submission. "
                    "Reusing the same key with the same payload returns the "
                    "existing transaction; reusing it with a different payload "
                    "returns 409 Conflict."
                ),
            )
        ],
        responses={
            200: TransactionReadSerializer,
            202: TransactionReadSerializer,
            400: OpenApiResponse(description="Missing or invalid SWIFT payload."),
            403: OpenApiResponse(
                description="You can transfer only from your own account."
            ),
            409: OpenApiResponse(
                description=(
                    "The Idempotency-Key is already used for a different SWIFT payload."
                )
            ),
        },
        description=(
            "Creates an asynchronous SWIFT transfer and stores beneficiary "
            "metadata. The transfer is accepted in pending status for later "
            "delayed processing."
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
            transaction, created = create_swift_transfer(
                transfer_input=SwiftTransferInput(
                    user=request.user,
                    from_account_iban=serializer.validated_data["from_account"].iban,
                    amount=serializer.validated_data["amount"],
                    idempotency_key=idempotency_key,
                    swift_code=serializer.validated_data["swift_code"],
                    beneficiary_name=serializer.validated_data["beneficiary_name"],
                    beneficiary_account_number=serializer.validated_data[
                        "beneficiary_account_number"
                    ],
                    beneficiary_iban=serializer.validated_data["beneficiary_iban"],
                    beneficiary_bank_name=serializer.validated_data[
                        "beneficiary_bank_name"
                    ],
                    beneficiary_bank_country=serializer.validated_data[
                        "beneficiary_bank_country"
                    ],
                    beneficiary_address=serializer.validated_data[
                        "beneficiary_address"
                    ],
                    swift_reference=serializer.validated_data["swift_reference"],
                    fraud_context=build_request_fraud_context(request),
                )
            )
        except TransactionPermissionError as exc:
            raise PermissionDenied(str(exc)) from exc
        except IdempotencyConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        response_serializer = TransactionReadSerializer(
            transaction,
            context=build_transaction_serializer_context(
                request,
                transaction=transaction,
            ),
        )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=response_status)


class TransactionChallengeConfirmView(generics.GenericAPIView):
    """Handle transaction 2FA confirmation API requests."""

    serializer_class = TransactionChallengeConfirmSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_write"

    def post(self, request, *args, **kwargs):
        """Confirm one pending transaction 2FA challenge."""

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            transaction = confirm_transaction_challenge(
                user=request.user,
                transaction_id=kwargs["pk"],
                code=serializer.validated_data["code"],
            )
        except TransactionPermissionError as exc:
            raise PermissionDenied(str(exc)) from exc
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        response_serializer = TransactionReadSerializer(
            transaction,
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)
