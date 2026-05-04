from django.core.cache import cache
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from core.cache_utils import build_user_cache_key, get_user_cache_version

from .selectors import list_user_accounts
from .serializers import (
    AccountCreateSerializer,
    AccountReadSerializer,
    RegisterSerializer,
)
from .services import build_auth_payload, create_account_for_user, register_user


class AccountListCreateView(generics.ListCreateAPIView):
    throttle_classes = [ScopedRateThrottle]
    def get_queryset(self):
        return list_user_accounts(user=self.request.user)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AccountCreateSerializer
        return AccountReadSerializer

    def get_throttles(self):
        self.throttle_scope = (
            "accounts_write" if self.request.method == "POST" else "accounts_read"
        )
        return super().get_throttles()

    def list(self, request, *args, **kwargs):
        version = get_user_cache_version(
            namespace="accounts_list",
            user_id=request.user.id,
        )
        cache_key = build_user_cache_key(
            namespace="accounts_list",
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

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        account = create_account_for_user(
            user=request.user,
            currency=serializer.validated_data["currency"],
        )
        response_serializer = AccountReadSerializer(account)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class RegisterView(generics.GenericAPIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = register_user(
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
            email=serializer.validated_data.get("email", ""),
        )
        return Response(
            build_auth_payload(user=user),
            status=status.HTTP_201_CREATED,
        )
