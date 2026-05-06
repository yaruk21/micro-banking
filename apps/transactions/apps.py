from django.apps import AppConfig


class TransactionsConfig(AppConfig):
    """Configure the transactions app."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.transactions"
