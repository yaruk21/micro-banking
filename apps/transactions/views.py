from celery import shared_task
from django.core.cache import cache
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from core.cache_utils import build_user_cache_key, get_user_cache_version

from .filters import TransactionFilter
from .selectors import list_user_transactions
from .serializers import TransactionCreateSerializer, TransactionReadSerializer
from .services import (
    TransferInput,
    TransactionPermissionError,
    TransactionValidationError,
    create_transfer,
)


class TransactionListCreateView(generics.ListCreateAPIView):
    filterset_class = TransactionFilter
    ordering_fields = ("created_at", "amount")
    throttle_classes = [ScopedRateThrottle]

    def get_queryset(self):
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

    @shared_task
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            transaction = create_transfer(
                transfer_input=TransferInput(
                    user=request.user,
                    from_account_iban=serializer.validated_data["from_account"].iban,
                    to_account_iban=serializer.validated_data["to_account"].iban,
                    amount=serializer.validated_data["amount"],
                )
            )
        except TransactionPermissionError as exc:
            raise PermissionDenied(str(exc)) from exc
        except TransactionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        response_serializer = TransactionReadSerializer(transaction)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
