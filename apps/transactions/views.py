from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

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

    def get_queryset(self):
        return list_user_transactions(user=self.request.user)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TransactionCreateSerializer
        return TransactionReadSerializer

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
