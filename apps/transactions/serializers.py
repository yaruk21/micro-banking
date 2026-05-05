from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import Account

from .models import Transaction


class TransactionCreateSerializer(serializers.Serializer):
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
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value


class TransactionReadSerializer(serializers.ModelSerializer):
    from_account_iban = serializers.CharField(source="from_account.iban", read_only=True)
    to_account_iban = serializers.CharField(source="to_account.iban", read_only=True)

    class Meta:
        model = Transaction
        fields = (
            "id",
            "from_account",
            "from_account_iban",
            "to_account",
            "to_account_iban",
            "amount",
            "status",
            "created_at",
            "processing_started_at",
            "completed_at",
            "failure_reason",
        )


class TransactionStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = (
            "id",
            "status",
            "created_at",
            "processing_started_at",
            "completed_at",
            "failure_reason",
        )
