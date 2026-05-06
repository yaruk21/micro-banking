import django_filters
from django.db.models import Q

from apps.transactions.models import Transaction


class TransactionFilter(django_filters.FilterSet):
    """Filter transaction query results."""
    account = django_filters.NumberFilter(method="filter_account")
    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        """Represent meta."""
        model = Transaction
        fields = ("account", "date_from", "date_to", "status")

    def filter_account(self, queryset, name, value):
        """Handle filter account."""
        return queryset.filter(
            Q(from_account_id=value) | Q(to_account_id=value)
        )
