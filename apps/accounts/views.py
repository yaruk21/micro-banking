from rest_framework import generics, status
from rest_framework.response import Response

from .selectors import list_user_accounts
from .serializers import AccountCreateSerializer, AccountReadSerializer
from .services import create_account_for_user


class AccountListCreateView(generics.ListCreateAPIView):
    def get_queryset(self):
        return list_user_accounts(user=self.request.user)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AccountCreateSerializer
        return AccountReadSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        account = create_account_for_user(
            user=request.user,
            currency=serializer.validated_data["currency"],
        )
        response_serializer = AccountReadSerializer(account)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
