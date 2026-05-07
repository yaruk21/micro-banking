import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.transactions.models import Transaction, TransactionReport
from apps.transactions.selectors import (
    list_user_transactions_for_period,
    summarize_user_transactions,
)
from core.structured_logging import log_event

from .exceptions import TransactionValidationError

logger = logging.getLogger("apps.transactions")


def create_transaction_report(*, user, date_from, date_to) -> TransactionReport:
    """Create one pending PDF report request and schedule background generation."""

    if date_from > date_to:
        raise TransactionValidationError(
            "date_to must be greater than or equal to date_from."
        )

    with db_transaction.atomic():
        report = TransactionReport.objects.create(
            user=user,
            date_from=date_from,
            date_to=date_to,
            file_name=_build_report_filename(
                user_id=user.id,
                date_from=date_from,
                date_to=date_to,
            ),
        )
        db_transaction.on_commit(
            lambda: _dispatch_transaction_report(report_id=report.id)
        )

    log_event(
        logger,
        logging.INFO,
        "transaction.report_requested",
        message="Transaction PDF report was accepted for background generation.",
        user_id=user.id,
        report_id=report.id,
        date_from=str(report.date_from),
        date_to=str(report.date_to),
        status=report.status,
    )
    return report


def process_transaction_report(*, report_id: int) -> TransactionReport:
    """Generate one transaction PDF report in the background."""

    with db_transaction.atomic():
        report = (
            TransactionReport.objects.select_for_update()
            .select_related("user")
            .filter(id=report_id)
            .first()
        )
        if report is None:
            raise TransactionValidationError("Transaction report does not exist.")

        if report.status == TransactionReport.Status.COMPLETED and report.pdf_content:
            return report

        report.status = TransactionReport.Status.PROCESSING
        report.processing_started_at = timezone.now()
        report.completed_at = None
        report.failure_reason = ""
        report.save(
            update_fields=[
                "status",
                "processing_started_at",
                "completed_at",
                "failure_reason",
            ]
        )

    try:
        summary = summarize_user_transactions(
            user=report.user,
            date_from=report.date_from,
            date_to=report.date_to,
            force_primary=True,
        )
        transactions = list(
            list_user_transactions_for_period(
                user=report.user,
                date_from=report.date_from,
                date_to=report.date_to,
                force_primary=True,
            )
            .order_by("created_at", "id")
        )
        pdf_content = _render_transaction_report_pdf(
            report=report,
            summary=summary,
            transactions=transactions,
        )
    except Exception as exc:
        return _mark_transaction_report_failed(
            report_id=report.id,
            reason=str(exc),
        )

    with db_transaction.atomic():
        completed_report = (
            TransactionReport.objects.select_for_update()
            .filter(id=report.id)
            .first()
        )
        if completed_report is None:
            raise TransactionValidationError("Transaction report does not exist.")

        completed_report.status = TransactionReport.Status.COMPLETED
        completed_report.completed_at = timezone.now()
        completed_report.failure_reason = ""
        completed_report.content_type = "application/pdf"
        completed_report.pdf_content = pdf_content
        completed_report.save(
            update_fields=[
                "status",
                "completed_at",
                "failure_reason",
                "content_type",
                "pdf_content",
            ]
        )

    log_event(
        logger,
        logging.INFO,
        "transaction.report_completed",
        message="Transaction PDF report was generated successfully.",
        user_id=completed_report.user_id,
        report_id=completed_report.id,
        date_from=str(completed_report.date_from),
        date_to=str(completed_report.date_to),
        status=completed_report.status,
    )
    return completed_report


def _dispatch_transaction_report(*, report_id: int) -> None:
    """Dispatch one background PDF report generation task after commit."""

    from apps.transactions.tasks import generate_transaction_report_task

    generate_transaction_report_task.delay(report_id)


def _mark_transaction_report_failed(
    *,
    report_id: int,
    reason: str,
) -> TransactionReport:
    """Persist one failed report generation result."""

    with db_transaction.atomic():
        report = (
            TransactionReport.objects.select_for_update()
            .filter(id=report_id)
            .first()
        )
        if report is None:
            raise TransactionValidationError("Transaction report does not exist.")

        report.status = TransactionReport.Status.FAILED
        report.completed_at = timezone.now()
        report.failure_reason = reason[:2000]
        report.pdf_content = None
        report.save(
            update_fields=[
                "status",
                "completed_at",
                "failure_reason",
                "pdf_content",
            ]
        )

    log_event(
        logger,
        logging.WARNING,
        "transaction.report_failed",
        message="Transaction PDF report generation failed.",
        user_id=report.user_id,
        report_id=report.id,
        status=report.status,
        failure_reason=report.failure_reason,
    )
    return report


def _build_report_filename(*, user_id: int, date_from, date_to) -> str:
    """Build a deterministic PDF filename for one report request."""

    return f"transaction-report-user-{user_id}-{date_from}-{date_to}.pdf"


def _render_transaction_report_pdf(*, report: TransactionReport, summary: dict, transactions) -> bytes:
    """Render a lightweight PDF report without external PDF dependencies."""

    lines = [
        "Micro-Banking Transaction Report",
        f"Report ID: {report.id}",
        f"User: {_ascii_text(report.user.username)}",
        f"Period: {report.date_from} to {report.date_to}",
        f"Generated at: {timezone.now().isoformat()}",
        "",
        "Summary",
        f"Total transactions: {summary['totals']['total_transactions']}",
        f"Pending: {summary['totals']['pending_transactions']}",
        f"Processing: {summary['totals']['processing_transactions']}",
        f"Completed: {summary['totals']['completed_transactions']}",
        f"Failed: {summary['totals']['failed_transactions']}",
        f"Completed outgoing: {summary['totals']['completed_outgoing_amount']}",
        f"Completed incoming: {summary['totals']['completed_incoming_amount']}",
        f"Net cashflow: {summary['totals']['net_completed_cashflow']}",
        "",
        "By currency",
    ]

    if summary["by_currency"]:
        for row in summary["by_currency"]:
            lines.append(
                (
                    f"{row['currency']}: out={row['completed_outgoing_amount']} "
                    f"in={row['completed_incoming_amount']} "
                    f"net={row['net_completed_cashflow']}"
                )
            )
    else:
        lines.append("No completed cashflow rows for the selected period.")

    lines.extend(["", "Transactions"])
    if transactions:
        for transaction in transactions:
            lines.append(_format_transaction_report_line(transaction=transaction))
    else:
        lines.append("No transactions found for the selected period.")

    return _build_simple_pdf(lines=lines)


def _format_transaction_report_line(*, transaction: Transaction) -> str:
    """Format one transaction row for the PDF body."""

    created_at = timezone.localtime(transaction.created_at).strftime(
        "%Y-%m-%d %H:%M"
    )
    recipient = transaction.to_account.iban if transaction.to_account else "external"
    credited_amount = transaction.credited_amount or transaction.amount
    line = (
        f"#{transaction.id} {created_at} {transaction.transfer_type} "
        f"{transaction.status} amount={transaction.amount} "
        f"credited={credited_amount} "
        f"{transaction.from_account.iban}->{recipient}"
    )
    return _ascii_text(line, limit=110)


def _build_simple_pdf(*, lines: list[str]) -> bytes:
    """Build a minimal multi-page PDF using the built-in Helvetica font."""

    page_width = 612
    page_height = 792
    lines_per_page = 48
    page_chunks = [
        lines[index:index + lines_per_page]
        for index in range(0, max(len(lines), 1), lines_per_page)
    ] or [[]]

    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    content_object_numbers: list[int] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    next_object_number = 4
    for page_number, page_lines in enumerate(page_chunks, start=1):
        content_stream = _build_pdf_page_stream(
            lines=page_lines + [f"Page {page_number} of {len(page_chunks)}"]
        )
        page_object_numbers.append(next_object_number)
        content_object_numbers.append(next_object_number + 1)
        objects.append(b"")
        objects.append(
            (
                f"<< /Length {len(content_stream)} >>\nstream\n"
            ).encode("latin-1")
            + content_stream
            + b"\nendstream"
        )
        next_object_number += 2

    page_kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[1] = (
        f"<< /Type /Pages /Count {len(page_object_numbers)} /Kids [{page_kids}] >>"
    ).encode("latin-1")

    for page_object_number, content_object_number in zip(
        page_object_numbers,
        content_object_numbers,
    ):
        objects[page_object_number - 1] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_object_number} 0 R >>"
        ).encode("latin-1")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, payload in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("latin-1"))
        pdf.extend(payload)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("latin-1")
    )
    return bytes(pdf)


def _build_pdf_page_stream(*, lines: list[str]) -> bytes:
    """Build one PDF text stream for a single page."""

    stream_lines = [
        "BT",
        "/F1 11 Tf",
        "14 TL",
        "50 760 Td",
    ]
    for index, line in enumerate(lines):
        if index:
            stream_lines.append("T*")
        stream_lines.append(f"({_escape_pdf_text(_ascii_text(line))}) Tj")
    stream_lines.append("ET")
    return "\n".join(stream_lines).encode("latin-1")


def _escape_pdf_text(value: str) -> str:
    """Escape special characters for a PDF literal string."""

    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ascii_text(value, *, limit: int = 200) -> str:
    """Normalize arbitrary text into a PDF-safe ASCII subset."""

    normalized = str(value or "").encode("ascii", "replace").decode("ascii")
    return normalized[:limit]
