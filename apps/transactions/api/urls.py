from django.urls import path

from .views import TransactionListCreateView, TransactionStatusView

urlpatterns = [
    path("", TransactionListCreateView.as_view(), name="transaction-list-create"),
    path("<int:pk>/status/", TransactionStatusView.as_view(), name="transaction-status"),
]
