from urllib.parse import urlencode

from rest_framework import serializers
from rest_framework.reverse import reverse

from apps.transactions.application.reporting import (
    build_transaction_report_download_token,
    report_has_downloadable_artifact,
)
from apps.transactions.models import TransactionReport


class TransactionReportCreateSerializer(serializers.Serializer):
    """Validate one asynchronous PDF report generation request."""

    date_from = serializers.DateField()
    date_to = serializers.DateField()

    def validate(self, attrs):
        """Validate the selected report date range."""

        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError(
                {"date_to": "date_to must be greater than or equal to date_from."}
            )
        return attrs


class TransactionReportReadSerializer(serializers.ModelSerializer):
    """Serialize transaction PDF report status metadata."""

    download_url = serializers.SerializerMethodField()

    class Meta:
        """Represent meta."""

        model = TransactionReport
        fields = (
            "id",
            "date_from",
            "date_to",
            "status",
            "file_name",
            "content_type",
            "failure_reason",
            "created_at",
            "processing_started_at",
            "completed_at",
            "download_url",
        )

    def get_download_url(self, obj: TransactionReport):
        """Return the report download URL only when the PDF is ready."""

        if (
            obj.status != TransactionReport.Status.COMPLETED
            or not report_has_downloadable_artifact(report=obj)
        ):
            return None

        request = self.context.get("request")
        if request is None:
            return None
        download_url = reverse(
            "transaction-report-download",
            kwargs={"pk": obj.id},
            request=request,
        )
        token = build_transaction_report_download_token(report=obj)
        return f"{download_url}?{urlencode({'token': token})}"
