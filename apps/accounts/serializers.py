from rest_framework import serializers

from .models import Account


class AccountCreateSerializer(serializers.Serializer):
    currency = serializers.ChoiceField(choices=Account.Currency.choices)


class AccountReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ("id", "iban", "balance", "currency", "created_at")
