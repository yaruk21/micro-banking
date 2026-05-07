from django.conf import settings
from django.core.cache import cache
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
)
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from core.cache_utils import build_user_cache_key, get_user_cache_version

from apps.transactions.selectors import summarize_user_transactions

from ..serializers.analytics import (
    TransactionAnalyticsQuerySerializer,
    TransactionAnalyticsSummarySerializer,
)


class TransactionAnalyticsSummaryView(generics.GenericAPIView):
    """Handle aggregated transaction analytics requests."""

    serializer_class = TransactionAnalyticsSummarySerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="date_from",
                type={"type": "string", "format": "date"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Inclusive lower bound for transaction created_at date.",
            ),
            OpenApiParameter(
                name="date_to",
                type={"type": "string", "format": "date"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Inclusive upper bound for transaction created_at date.",
            ),
        ],
        responses={200: TransactionAnalyticsSummarySerializer},
        description=(
            "Returns aggregated transaction counts and completed cashflow totals "
            "for the authenticated user within the requested period."
        ),
    )
    def get(self, request, *args, **kwargs):
        """Return cached transaction analytics summary."""

        query_serializer = TransactionAnalyticsQuerySerializer(
            data=request.query_params
        )
        query_serializer.is_valid(raise_exception=True)

        version = get_user_cache_version(
            namespace="transactions_list",
            user_id=request.user.id,
        )
        cache_key = build_user_cache_key(
            namespace="transactions_list",
            user_id=request.user.id,
            version=version,
            suffix=f"analytics-summary:{request.get_full_path()}",
        )
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        response_payload = summarize_user_transactions(
            user=request.user,
            date_from=query_serializer.validated_data.get("date_from"),
            date_to=query_serializer.validated_data.get("date_to"),
        )
        response_data = self.get_serializer(response_payload).data
        cache.set(
            cache_key,
            response_data,
            timeout=settings.LIST_CACHE_TIMEOUT_SECONDS,
        )
        return Response(response_data)
