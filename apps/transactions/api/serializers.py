from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import Account
from apps.transactions.models import (
    COUNTRY_CODE_VALIDATOR,
    IBAN_VALIDATOR,
    SWIFT_CODE_VALIDATOR,
    Transaction,
    TransactionBatch,
    TransactionBatchItem,
    SwiftTransferDetails,
)


class TransactionCreateSerializer(serializers.Serializer):
    """Serialize and validate transaction create data."""
    from_account_iban = serializers.SlugRelatedField(
        queryset=Account.objects.all(),
        slug_field="iban",
        source="from_account",
    )
    to_account_iban = serializers.SlugRelatedField(
        queryset=Account.objects.all(),
        slug_field="iban",
        source="to_account",
    )
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)

    def validate_amount(self, value: Decimal) -> Decimal:
        """Validate amount."""
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value


class SwiftTransactionCreateSerializer(serializers.Serializer):
    """Serialize and validate SWIFT transaction create data."""

    from_account_iban = serializers.SlugRelatedField(
        queryset=Account.objects.all(),
        slug_field="iban",
        source="from_account",
    )
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    swift_code = serializers.CharField(max_length=11, validators=[SWIFT_CODE_VALIDATOR])
    beneficiary_name = serializers.CharField(max_length=255)
    beneficiary_account_number = serializers.CharField(max_length=34)
    beneficiary_iban = serializers.CharField(max_length=34, allow_blank=True, required=False, default="",)
    beneficiary_bank_name = serializers.CharField(max_length=255)
    beneficiary_bank_country = serializers.CharField( max_length=2, validators=[COUNTRY_CODE_VALIDATOR],)
    beneficiary_address = serializers.CharField(allow_blank=True, required=False,default="",)
    swift_reference = serializers.CharField(max_length=64, allow_blank=True, required=False, default="",)

    def validate_amount(self, value: Decimal) -> Decimal:
        """Validate amount."""
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate_beneficiary_iban(self, value: str) -> str:
        """Validate beneficiary IBAN only when it is provided."""

        if not value:
            return value
        IBAN_VALIDATOR(value)
        return value


class SwiftTransferDetailsReadSerializer(serializers.Serializer):
    """Serialize SWIFT-specific transaction details."""

    swift_code = serializers.CharField()
    beneficiary_name = serializers.CharField()
    beneficiary_account_number = serializers.CharField()
    beneficiary_iban = serializers.CharField()
    beneficiary_bank_name = serializers.CharField()
    beneficiary_bank_country = serializers.CharField()
    beneficiary_address = serializers.CharField()
    swift_reference = serializers.CharField()
    scheduled_processing_at = serializers.DateTimeField()
    expected_completion_at = serializers.DateTimeField()
    swift_fee_fixed = serializers.DecimalField(max_digits=18, decimal_places=2)
    swift_fee_rate = serializers.DecimalField(max_digits=6, decimal_places=4)


class TransactionReadSerializer(serializers.ModelSerializer):
    """Serialize and validate transaction read data."""
    from_account_iban = serializers.CharField(source="from_account.iban", read_only=True)
    from_account_currency = serializers.CharField(
        source="from_account.currency",
        read_only=True,
    )
    to_account_iban = serializers.SerializerMethodField()
    to_account_currency = serializers.SerializerMethodField()
    swift_details = serializers.SerializerMethodField()

    class Meta:
        """Represent meta."""
        model = Transaction
        fields = (
            "id",
            "from_account",
            "from_account_iban",
            "from_account_currency",
            "to_account",
            "to_account_iban",
            "to_account_currency",
            "amount",
            "credited_amount",
            "exchange_rate",
            "exchange_rate_provider",
            "fee_amount",
            "fee_currency",
            "transfer_type",
            "status",
            "created_at",
            "processing_started_at",
            "completed_at",
            "failure_reason",
            "swift_details",
        )

    def get_to_account_iban(self, obj: Transaction):
        """Return recipient IBAN when the recipient is a local account."""

        return obj.to_account.iban if obj.to_account is not None else None

    def get_to_account_currency(self, obj: Transaction):
        """Return recipient currency when the recipient is a local account."""

        return obj.to_account.currency if obj.to_account is not None else None

    def get_swift_details(self, obj: Transaction):
        """Return serialized SWIFT details when available."""

        details = _get_swift_details(obj)
        if details is None:
            return None
        return SwiftTransferDetailsReadSerializer(details).data


class TransactionStatusSerializer(serializers.ModelSerializer):
    """Serialize and validate transaction status data."""
    scheduled_processing_at = serializers.SerializerMethodField()
    expected_completion_at = serializers.SerializerMethodField()

    class Meta:
        """Represent meta."""
        model = Transaction
        fields = (
            "id",
            "transfer_type",
            "status",
            "created_at",
            "processing_started_at",
            "completed_at",
            "failure_reason",
            "scheduled_processing_at",
            "expected_completion_at",
        )

    def get_scheduled_processing_at(self, obj: Transaction):
        """Return the planned SWIFT processing timestamp when available."""

        details = _get_swift_details(obj)
        if details is None:
            return None
        return serializers.DateTimeField().to_representation(
            details.scheduled_processing_at
        )

    def get_expected_completion_at(self, obj: Transaction):
        """Return the planned SWIFT completion timestamp when available."""

        details = _get_swift_details(obj)
        if details is None:
            return None
        return serializers.DateTimeField().to_representation(
            details.expected_completion_at
        )


def _get_swift_details(transaction: Transaction):
    """Return reverse one-to-one SWIFT details when present."""

    try:
        return transaction.swift_details
    except SwiftTransferDetails.DoesNotExist:
        return None


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
    transaction_id = serializers.IntegerField(source="transaction.id", read_only=True)
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
