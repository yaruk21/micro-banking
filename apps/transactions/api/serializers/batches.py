from decimal import Decimal

from rest_framework import serializers

from apps.transactions.models import TransactionBatch, TransactionBatchItem


class TransactionBatchItemCreateSerializer(serializers.Serializer):
    """Serialize and validate transaction batch item create data."""

    from_account_iban = serializers.CharField(max_length=34)
    to_account_iban = serializers.CharField(max_length=34)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    idempotency_key = serializers.CharField(max_length=255)

    def validate_amount(self, value: Decimal) -> Decimal:
        """Validate amount."""

        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value


class TransactionBatchCreateSerializer(serializers.Serializer):
    """Serialize and validate transaction batch create data."""

    items = TransactionBatchItemCreateSerializer(many=True)

    def validate_items(self, value):
        """Validate items."""

        if not value:
            raise serializers.ValidationError(
                "Batch must contain at least one transaction."
            )
        if len(value) > 1000:
            raise serializers.ValidationError(
                "Batch size cannot exceed 1000 transactions."
            )

        idempotency_keys = [item["idempotency_key"] for item in value]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise serializers.ValidationError(
                "Each batch item must use a unique idempotency_key."
            )
        return value


class TransactionBatchItemReadSerializer(serializers.ModelSerializer):
    """Serialize and validate transaction batch item read data."""

    transaction_id = serializers.IntegerField(
        source="transaction.id",
        read_only=True,
    )
    transaction_status = serializers.CharField(
        source="transaction.status",
        read_only=True,
    )

    class Meta:
        """Represent meta."""

        model = TransactionBatchItem
        fields = (
            "id",
            "sequence",
            "from_account_iban",
            "to_account_iban",
            "amount",
            "idempotency_key",
            "transaction_id",
            "transaction_status",
            "created_transaction",
            "error_message",
        )


class TransactionBatchReadSerializer(serializers.ModelSerializer):
    """Serialize and validate transaction batch read data."""

    items = TransactionBatchItemReadSerializer(many=True, read_only=True)

    class Meta:
        """Represent meta."""

        model = TransactionBatch
        fields = (
            "id",
            "status",
            "total_items",
            "processed_items",
            "succeeded_items",
            "failed_items",
            "created_at",
            "processing_started_at",
            "completed_at",
            "failure_reason",
            "items",
        )
