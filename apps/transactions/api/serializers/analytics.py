from rest_framework import serializers

from apps.transactions.models import Transaction


class TransactionAnalyticsQuerySerializer(serializers.Serializer):
    """Validate query params for transaction analytics summary."""

    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        """Validate analytics period bounds."""

        if (
            attrs.get("date_from") is not None
            and attrs.get("date_to") is not None
            and attrs["date_from"] > attrs["date_to"]
        ):
            raise serializers.ValidationError(
                {"date_to": "date_to must be greater than or equal to date_from."}
            )
        return attrs


class TransactionAnalyticsPeriodSerializer(serializers.Serializer):
    """Serialize the effective analytics period."""

    date_from = serializers.DateField(allow_null=True)
    date_to = serializers.DateField(allow_null=True)


class TransactionAnalyticsTotalsSerializer(serializers.Serializer):
    """Serialize aggregate transaction counters and cashflow totals."""

    total_transactions = serializers.IntegerField()
    pending_transactions = serializers.IntegerField()
    processing_transactions = serializers.IntegerField()
    completed_transactions = serializers.IntegerField()
    failed_transactions = serializers.IntegerField()
    completed_outgoing_amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )
    completed_incoming_amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )
    net_completed_cashflow = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )


class TransactionAnalyticsCurrencySerializer(serializers.Serializer):
    """Serialize per-currency cashflow aggregates."""

    currency = serializers.CharField()
    outgoing_transactions = serializers.IntegerField()
    incoming_transactions = serializers.IntegerField()
    completed_outgoing_amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )
    completed_incoming_amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )
    net_completed_cashflow = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
    )


class TransactionAnalyticsTransferTypeSerializer(serializers.Serializer):
    """Serialize aggregates per transfer rail."""

    transfer_type = serializers.ChoiceField(
        choices=Transaction.TransferType.choices
    )
    total_transactions = serializers.IntegerField()
    completed_transactions = serializers.IntegerField()
    total_amount = serializers.DecimalField(max_digits=18, decimal_places=2)


class TransactionAnalyticsSummarySerializer(serializers.Serializer):
    """Serialize the transaction analytics summary payload."""

    period = TransactionAnalyticsPeriodSerializer()
    totals = TransactionAnalyticsTotalsSerializer()
    by_currency = TransactionAnalyticsCurrencySerializer(many=True)
    by_transfer_type = TransactionAnalyticsTransferTypeSerializer(many=True)
