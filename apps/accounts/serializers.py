from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Account

User = get_user_model()


class AccountCreateSerializer(serializers.Serializer):
    currency = serializers.ChoiceField(choices=Account.Currency.choices)


class AccountReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ("id", "iban", "balance", "currency", "created_at")


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    def validate_username(self, value: str) -> str:
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError(
                "A user with this username already exists."
            )
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )

        validate_password(attrs["password"])
        return attrs
