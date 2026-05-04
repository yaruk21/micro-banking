from django.contrib import admin

from .models import Account


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "iban", "owner", "currency", "balance", "created_at")
    search_fields = ("iban", "owner__username", "owner__email")
    list_filter = ("currency", "created_at")
