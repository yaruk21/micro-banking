from django.urls import path

from .views import (
    TransactionAnalyticsSummaryView,
    TransactionBatchCreateView,
    TransactionBatchStatusView,
    TransactionChallengeConfirmView,
    TransactionListCreateView,
    TransactionReportCreateView,
    TransactionReportDownloadView,
    TransactionReportStatusView,
    TransactionSwiftCreateView,
    TransactionStatusView,
)

urlpatterns = [
    path("", TransactionListCreateView.as_view(), name="transaction-list-create"),
    path(
        "analytics/summary/",
        TransactionAnalyticsSummaryView.as_view(),
        name="transaction-analytics-summary",
    ),
    path(
        "reports/",
        TransactionReportCreateView.as_view(),
        name="transaction-report-create",
    ),
    path(
        "reports/<int:pk>/",
        TransactionReportStatusView.as_view(),
        name="transaction-report-status",
    ),
    path(
        "reports/<int:pk>/download/",
        TransactionReportDownloadView.as_view(),
        name="transaction-report-download",
    ),
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
