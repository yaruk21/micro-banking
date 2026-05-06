from django.contrib import admin

from .models import Transaction


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """Configure admin integration for transaction."""
    list_display = (
        "id",
        "from_account",
        "to_account",
        "amount",
        "status",
        "created_at",
    )
    search_fields = ("from_account__iban", "to_account__iban", "failure_reason")
    list_filter = ("status", "created_at")
