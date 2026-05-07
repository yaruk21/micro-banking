from decimal import Decimal, InvalidOperation

from apps.transactions.application import RequestFraudContext
from apps.transactions.application.challenge import (
    expose_transaction_challenge_code,
    get_debug_transaction_challenge_code,
)


def build_request_fraud_context(request) -> RequestFraudContext:
    """Extract request metadata used by behavioral fraud checks."""

    return RequestFraudContext(
        request_id=str(getattr(request, "request_id", "") or "").strip(),
        ip_address=extract_client_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
        country_code=first_header_value(request, "X-Country-Code", "CF-IPCountry"),
        region=first_header_value(request, "X-Region"),
        city=first_header_value(request, "X-City"),
        latitude=parse_decimal_header(first_header_value(request, "X-Latitude")),
        longitude=parse_decimal_header(first_header_value(request, "X-Longitude")),
    )


def extract_client_ip(request) -> str:
    """Return the best-effort client IP address for the current request."""

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.headers.get("X-Real-IP", "").strip() or str(
        request.META.get("REMOTE_ADDR", "")
    ).strip()


def first_header_value(request, *header_names: str) -> str:
    """Return the first non-empty request header value."""

    for header_name in header_names:
        value = request.headers.get(header_name, "").strip()
        if value:
            return value
    return ""


def parse_decimal_header(raw_value: str):
    """Parse an optional decimal header without failing the request."""

    value = str(raw_value or "").strip()
    if not value:
        return None

    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def build_transaction_serializer_context(request, *, transaction=None) -> dict:
    """Build serializer context for transaction responses."""

    challenge_code = None
    if transaction is not None and expose_transaction_challenge_code():
        challenge_code = get_debug_transaction_challenge_code(transaction=transaction)
    return {
        "request": request,
        "include_challenge_code": bool(challenge_code),
        "challenge_code": challenge_code,
    }
