from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.transactions.application import create_transaction_report
from apps.transactions.application.reporting import (
    build_transaction_report_storage_download_url,
    open_transaction_report_file,
    report_has_downloadable_artifact,
    validate_transaction_report_download_token,
)
from apps.transactions.models import TransactionReport

from ..serializers.reports import (
    TransactionReportCreateSerializer,
    TransactionReportReadSerializer,
)


class TransactionReportCreateView(generics.GenericAPIView):
    """Handle asynchronous transaction PDF report requests."""

    serializer_class = TransactionReportCreateSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_write"

    @extend_schema(
        request=TransactionReportCreateSerializer,
        responses={202: TransactionReportReadSerializer},
        description=(
            "Queues one PDF transaction report for the selected period and "
            "returns its asynchronous status resource."
        ),
    )
    def post(self, request, *args, **kwargs):
        """Queue one PDF transaction report request."""

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = create_transaction_report(
            user=request.user,
            date_from=serializer.validated_data["date_from"],
            date_to=serializer.validated_data["date_to"],
        )
        response_serializer = TransactionReportReadSerializer(
            report,
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)


class TransactionReportStatusView(generics.GenericAPIView):
    """Handle transaction PDF report status requests."""

    serializer_class = TransactionReportReadSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={
            200: TransactionReportReadSerializer,
            404: OpenApiResponse(description="Transaction report not found."),
        },
        description="Returns the current asynchronous PDF report generation status.",
    )
    def get(self, request, *args, **kwargs):
        """Return one report status resource."""

        report = get_user_transaction_report_or_404(
            user=request.user,
            report_id=kwargs["pk"],
        )
        serializer = self.get_serializer(
            report,
            context={"request": request},
        )
        return Response(serializer.data)


class TransactionReportDownloadView(generics.GenericAPIView):
    """Handle completed transaction PDF report downloads."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "transactions_read"

    @extend_schema(
        responses={
            200: OpenApiResponse(description="PDF report binary response."),
            404: OpenApiResponse(description="Transaction report not found."),
            409: OpenApiResponse(
                description="Transaction report is not ready yet."
            ),
        },
        description=(
            "Downloads the generated PDF report when background generation is "
            "complete."
        ),
    )
    def get(self, request, *args, **kwargs):
        """Download one completed PDF report."""

        report = get_authorized_transaction_report_or_404(
            request=request,
            report_id=kwargs["pk"],
        )
        if (
            report.status != TransactionReport.Status.COMPLETED
            or not report_has_downloadable_artifact(report=report)
        ):
            return Response(
                {"detail": "Transaction report is not ready for download."},
                status=status.HTTP_409_CONFLICT,
            )

        if report.storage_key:
            storage_download_url = build_transaction_report_storage_download_url(
                report=report
            )
            if storage_download_url:
                return redirect(storage_download_url)
            try:
                report_file = open_transaction_report_file(report=report)
            except OSError:
                return Response(
                    {"detail": "Transaction report file is unavailable."},
                    status=status.HTTP_409_CONFLICT,
                )
            return FileResponse(
                report_file,
                as_attachment=True,
                filename=report.file_name or f"transaction-report-{report.id}.pdf",
                content_type=report.content_type or "application/pdf",
            )

        response = HttpResponse(
            report.pdf_content,
            content_type=report.content_type or "application/pdf",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{report.file_name or f"transaction-report-{report.id}.pdf"}"'
        )
        return response


def get_user_transaction_report_or_404(*, user, report_id: int) -> TransactionReport:
    """Return one report owned by the current user or raise 404."""

    report = TransactionReport.objects.filter(id=report_id, user=user).first()
    if report is None:
        raise NotFound("Transaction report not found.")
    return report


def get_authorized_transaction_report_or_404(*, request, report_id: int) -> TransactionReport:
    """Return one report when the current user or a signed token is authorized."""

    user = request.user
    if user.is_authenticated:
        report = TransactionReport.objects.filter(id=report_id, user=user).first()
        if report is not None:
            return report

    token = request.query_params.get("token", "")
    if validate_transaction_report_download_token(report_id=report_id, token=token):
        report = TransactionReport.objects.filter(id=report_id).first()
        if report is not None:
            return report

    raise NotFound("Transaction report not found.")
