import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional
from typing import Iterable

from django.conf import settings
from django.db.models import Avg, Count, Max
from django.utils import timezone

from apps.transactions.models import FraudEvent, Transaction
from core.structured_logging import log_event

from .exceptions import TransactionFraudBlockedError
from .types import RequestFraudContext

logger = logging.getLogger("apps.transactions")
EARTH_RADIUS_KM = 6371.0

ACTION_ALLOW = "allow"
ACTION_FLAG = "flag"
ACTION_CHALLENGE = "challenge"
ACTION_BLOCK = "block"

ACTION_PRIORITY = {
    ACTION_ALLOW: 0,
    ACTION_FLAG: 1,
    ACTION_CHALLENGE: 2,
    ACTION_BLOCK: 3,
}
SUPPORTED_ACTIONS = set(ACTION_PRIORITY)


@dataclass(frozen=True)
class FraudRuleMatch:
    """Represent one matched fraud rule."""

    rule_code: str
    action: str
    reason: str


@dataclass(frozen=True)
class FraudDecision:
    """Represent the aggregated fraud decision."""

    action: str
    outcome: str
    reasons: tuple[str, ...]
    rule_codes: tuple[str, ...]

    @property
    def should_block(self) -> bool:
        """Return whether the request must be blocked."""

        return self.action == ACTION_BLOCK


def evaluate_transaction_attempt(
    *,
    user,
    amount: Decimal,
    fraud_context: Optional[RequestFraudContext],
    now=None,
) -> FraudDecision:
    """Evaluate behavioral fraud rules for a transaction creation attempt."""

    context = fraud_context or RequestFraudContext()
    reference_time = now or timezone.now()
    matches: list[FraudRuleMatch] = []

    frequency_match = _match_frequency_rule(user=user, now=reference_time)
    if frequency_match is not None:
        matches.append(frequency_match)

    amount_match = _match_amount_anomaly_rule(
        user=user,
        amount=amount,
    )
    if amount_match is not None:
        matches.append(amount_match)

    geolocation_match = _match_geolocation_rule(
        user=user,
        fraud_context=context,
        now=reference_time,
    )
    if geolocation_match is not None:
        matches.append(geolocation_match)

    return _build_decision(matches)


def create_transaction_attempt_event(
    *,
    user,
    amount: Decimal,
    fraud_context: Optional[RequestFraudContext],
    transaction=None,
    now=None,
) -> tuple[FraudEvent, FraudDecision]:
    """Persist one fraud/activity event for a transaction creation attempt."""

    context = fraud_context or RequestFraudContext()
    decision = evaluate_transaction_attempt(
        user=user,
        amount=amount,
        fraud_context=context,
        now=now,
    )
    fraud_event = FraudEvent.objects.create(
        user=user,
        transaction=transaction,
        request_id=context.request_id.strip()[:255],
        event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
        outcome=decision.outcome,
        ip_address=_normalize_ip_address(context.ip_address),
        user_agent=context.user_agent.strip()[:512],
        country_code=_normalize_country_code(context.country_code),
        region=context.region.strip()[:100],
        city=context.city.strip()[:100],
        latitude=context.latitude,
        longitude=context.longitude,
    )

    if decision.action != ACTION_ALLOW:
        log_event(
            logger,
            logging.WARNING,
            "transaction.fraud_behavior_detected",
            message="Behavioral fraud rules matched during transaction creation.",
            user_id=user.id,
            request_id=fraud_event.request_id or None,
            fraud_action=decision.action,
            fraud_outcome=decision.outcome,
            fraud_rule_codes=list(decision.rule_codes),
            fraud_reasons=list(decision.reasons),
        )

    return fraud_event, decision


def attach_fraud_event_transaction(
    *,
    fraud_event: Optional[FraudEvent],
    transaction,
) -> None:
    """Attach the accepted transaction to the previously saved fraud event."""

    if fraud_event is None or transaction is None or fraud_event.transaction_id == transaction.id:
        return

    fraud_event.transaction = transaction
    fraud_event.save(update_fields=["transaction"])


def raise_for_fraud_decision(*, decision: FraudDecision) -> None:
    """Raise when the aggregated fraud decision blocks the request."""

    if decision.should_block:
        raise TransactionFraudBlockedError(
            "Transaction was blocked by behavioral fraud checks."
        )


def _match_frequency_rule(*, user, now) -> Optional[FraudRuleMatch]:
    """Detect suspicious bursts of transaction creation attempts."""

    max_attempts = max(int(settings.FRAUD_FREQUENCY_MAX_ATTEMPTS), 0)
    if max_attempts <= 0:
        return None

    window_seconds = max(int(settings.FRAUD_FREQUENCY_WINDOW_SECONDS), 1)
    attempts_in_window = FraudEvent.objects.filter(
        user=user,
        event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
        created_at__gte=now - timedelta(seconds=window_seconds),
    ).count()
    current_attempt_count = attempts_in_window + 1
    if current_attempt_count <= max_attempts:
        return None

    return FraudRuleMatch(
        rule_code="frequency_burst",
        action=_normalize_action(settings.FRAUD_FREQUENCY_ACTION, default=ACTION_FLAG),
        reason=(
            f"{current_attempt_count} transaction attempts within "
            f"{window_seconds} seconds exceed the configured limit of {max_attempts}."
        ),
    )


def _match_amount_anomaly_rule(*, user, amount: Decimal) -> Optional[FraudRuleMatch]:
    """Detect transaction amounts that are sharply above the user's baseline."""

    min_history_count = max(int(settings.FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS), 0)
    if min_history_count <= 0:
        return None

    stats = Transaction.objects.filter(
        initiated_by=user,
    ).exclude(
        status=Transaction.Status.FAILED,
    ).aggregate(
        historical_count=Count("id"),
        average_amount=Avg("amount"),
        max_amount=Max("amount"),
    )
    historical_count = int(stats.get("historical_count") or 0)
    if historical_count < min_history_count:
        return None

    average_amount = stats.get("average_amount")
    max_amount = stats.get("max_amount")
    if average_amount is None or max_amount is None or average_amount <= Decimal("0.00"):
        return None

    multiplier = settings.FRAUD_AMOUNT_ANOMALY_MULTIPLIER
    baseline_threshold = average_amount * multiplier
    if amount <= baseline_threshold:
        return None

    if amount <= max_amount:
        return None

    return FraudRuleMatch(
        rule_code="amount_anomaly",
        action=_normalize_action(settings.FRAUD_AMOUNT_ACTION, default=ACTION_FLAG),
        reason=(
            f"Requested amount {amount} exceeds the configured baseline threshold "
            f"of {baseline_threshold:.2f} based on average historical amount "
            f"{average_amount:.2f} across {historical_count} transactions."
        ),
    )


def _match_geolocation_rule(
    *,
    user,
    fraud_context: RequestFraudContext,
    now,
) -> Optional[FraudRuleMatch]:
    """Detect abrupt country changes or impossible travel between attempts."""

    if not _has_geolocation_signal(fraud_context):
        return None

    previous_event = (
        FraudEvent.objects.filter(
            user=user,
            event_type=FraudEvent.EventType.TRANSACTION_ATTEMPT,
        )
        .exclude(country_code="", latitude__isnull=True, longitude__isnull=True)
        .only("country_code", "latitude", "longitude", "created_at", "id")
        .order_by("-created_at", "-id")
        .first()
    )
    if previous_event is None:
        return None

    country_change_match = _build_country_change_match(
        previous_event=previous_event,
        fraud_context=fraud_context,
        now=now,
    )
    if country_change_match is not None:
        return country_change_match

    return _build_impossible_travel_match(
        previous_event=previous_event,
        fraud_context=fraud_context,
        now=now,
    )


def _build_country_change_match(
    *,
    previous_event,
    fraud_context,
    now,
) -> Optional[FraudRuleMatch]:
    """Detect sharp country changes within a short time window."""

    current_country = _normalize_country_code(fraud_context.country_code)
    previous_country = _normalize_country_code(previous_event.country_code)
    if not current_country or not previous_country or current_country == previous_country:
        return None

    window_seconds = max(int(settings.FRAUD_GEO_COUNTRY_CHANGE_WINDOW_SECONDS), 1)
    elapsed_seconds = max((now - previous_event.created_at).total_seconds(), 0)
    if elapsed_seconds > window_seconds:
        return None

    return FraudRuleMatch(
        rule_code="country_change",
        action=_normalize_action(settings.FRAUD_GEO_ACTION, default=ACTION_FLAG),
        reason=(
            f"Country changed from {previous_country} to {current_country} "
            f"within {int(elapsed_seconds)} seconds."
        ),
    )


def _build_impossible_travel_match(
    *,
    previous_event,
    fraud_context,
    now,
) -> Optional[FraudRuleMatch]:
    """Detect impossible travel between two geo-tagged requests."""

    if (
        previous_event.latitude is None
        or previous_event.longitude is None
        or fraud_context.latitude is None
        or fraud_context.longitude is None
    ):
        return None

    elapsed_hours = (now - previous_event.created_at).total_seconds() / 3600
    if elapsed_hours <= 0:
        return None

    distance_km = _calculate_distance_km(
        latitude_a=float(previous_event.latitude),
        longitude_a=float(previous_event.longitude),
        latitude_b=float(fraud_context.latitude),
        longitude_b=float(fraud_context.longitude),
    )
    if distance_km <= 0:
        return None

    speed_kmh = distance_km / elapsed_hours
    max_speed_kmh = max(int(settings.FRAUD_GEO_IMPOSSIBLE_TRAVEL_SPEED_KMH), 1)
    if speed_kmh <= max_speed_kmh:
        return None

    return FraudRuleMatch(
        rule_code="impossible_travel",
        action=_normalize_action(settings.FRAUD_GEO_ACTION, default=ACTION_FLAG),
        reason=(
            f"Implied travel speed of {speed_kmh:.1f} km/h exceeds "
            f"the configured limit of {max_speed_kmh} km/h."
        ),
    )


def _build_decision(matches: Iterable[FraudRuleMatch]) -> FraudDecision:
    """Collapse matched rules into one decision for the current attempt."""

    collected_matches = list(matches)
    if not collected_matches:
        return FraudDecision(
            action=ACTION_ALLOW,
            outcome=FraudEvent.Outcome.ALLOWED,
            reasons=(),
            rule_codes=(),
        )

    highest_match = max(
        collected_matches,
        key=lambda match: ACTION_PRIORITY[match.action],
    )
    return FraudDecision(
        action=highest_match.action,
        outcome=_action_to_outcome(highest_match.action),
        reasons=tuple(match.reason for match in collected_matches),
        rule_codes=tuple(match.rule_code for match in collected_matches),
    )


def _action_to_outcome(action: str) -> str:
    """Map future fraud actions to the currently persisted outcome enum."""

    if action == ACTION_BLOCK:
        return FraudEvent.Outcome.BLOCKED
    if action in {ACTION_FLAG, ACTION_CHALLENGE}:
        return FraudEvent.Outcome.FLAGGED
    return FraudEvent.Outcome.ALLOWED


def _normalize_action(raw_action: str, *, default: str) -> str:
    """Normalize a configured fraud action value."""

    normalized_action = str(raw_action).strip().lower()
    if normalized_action not in SUPPORTED_ACTIONS:
        return default
    return normalized_action


def _normalize_country_code(value: str) -> str:
    """Normalize an ISO-like country code for persistence and comparison."""

    return str(value or "").strip().upper()[:2]


def _normalize_ip_address(value: str) -> Optional[str]:
    """Normalize an optional IP address before persisting."""

    normalized_ip = str(value or "").strip()
    return normalized_ip or None


def _has_geolocation_signal(fraud_context: RequestFraudContext) -> bool:
    """Return whether the request carries any geolocation signal."""

    if _normalize_country_code(fraud_context.country_code):
        return True
    return fraud_context.latitude is not None and fraud_context.longitude is not None


def _calculate_distance_km(
    *,
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Calculate the great-circle distance between two coordinates."""

    lat_a = math.radians(latitude_a)
    lon_a = math.radians(longitude_a)
    lat_b = math.radians(latitude_b)
    lon_b = math.radians(longitude_b)

    delta_lat = lat_b - lat_a
    delta_lon = lon_b - lon_a
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    arc = 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))
    return EARTH_RADIUS_KM * arc
