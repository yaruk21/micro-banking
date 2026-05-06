from django.urls import path

from .views import (
    TransactionBatchCreateView,
    TransactionBatchStatusView,
    TransactionChallengeConfirmView,
    TransactionListCreateView,
    TransactionSwiftCreateView,
    TransactionStatusView,
)

urlpatterns = [
    path("", TransactionListCreateView.as_view(), name="transaction-list-create"),
    path("swift/", TransactionSwiftCreateView.as_view(), name="transaction-swift-create"),
    path("batches/", TransactionBatchCreateView.as_view(), name="transaction-batch-create"),
    path(
        "<int:pk>/challenge/confirm/",
        TransactionChallengeConfirmView.as_view(),
        name="transaction-challenge-confirm",
    ),
    path(
        "batches/<int:pk>/status/",
        TransactionBatchStatusView.as_view(),
        name="transaction-batch-status",
    ),
    path("<int:pk>/status/", TransactionStatusView.as_view(), name="transaction-status"),
]
