from decimal import Decimal

from django.core.validators import RegexValidator
from django.db import models

SWIFT_CODE_VALIDATOR = RegexValidator(
    regex=r"^[A-Z0-9]{8}([A-Z0-9]{3})?$",
    message="SWIFT code must contain 8 or 11 uppercase letters and digits.",
)
COUNTRY_CODE_VALIDATOR = RegexValidator(
    regex=r"^[A-Z]{2}$",
    message="Bank country must be a 2-letter ISO country code.",
)
IBAN_VALIDATOR = RegexValidator(
    regex=r"^[A-Z0-9]{15,34}$",
    message="IBAN must contain 15 to 34 uppercase letters and digits.",
)


class SwiftTransferDetails(models.Model):
    """Stores SWIFT-specific recipient and settlement metadata for one transfer."""

    transaction = models.OneToOneField(
        "transactions.Transaction",
        on_delete=models.CASCADE,
        related_name="swift_details",
        db_constraint=False,
    )
    swift_code = models.CharField(
        max_length=11,
        validators=[SWIFT_CODE_VALIDATOR],
        db_index=True,
    )
    beneficiary_name = models.CharField(max_length=255, db_index=True)
    beneficiary_account_number = models.CharField(max_length=34)
    beneficiary_iban = models.CharField(
        max_length=34,
        blank=True,
        validators=[IBAN_VALIDATOR],
    )
    beneficiary_bank_name = models.CharField(max_length=255, db_index=True)
    beneficiary_bank_country = models.CharField(
        max_length=2,
        validators=[COUNTRY_CODE_VALIDATOR],
        db_index=True,
    )
    beneficiary_address = models.TextField(blank=True)
    swift_reference = models.CharField(max_length=64, blank=True, db_index=True)
    scheduled_processing_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )
    expected_completion_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )
    swift_fee_fixed = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("10.00"),
    )
    swift_fee_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.0100"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Represent meta."""

        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                check=models.Q(swift_fee_fixed__gte=Decimal("0.00")),
                name="swift_details_fee_fixed_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(swift_fee_rate__gte=Decimal("0.0000")),
                name="swift_details_fee_rate_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(swift_fee_rate__lte=Decimal("1.0000")),
                name="swift_details_fee_rate_lte_one",
            ),
        ]

    def save(self, *args, **kwargs):
        """Normalize canonical SWIFT fields before persisting."""

        self.swift_code = self.swift_code.replace(" ", "").upper().strip()
        self.beneficiary_account_number = (
            self.beneficiary_account_number.replace(" ", "").upper().strip()
        )
        self.beneficiary_iban = self.beneficiary_iban.replace(" ", "").upper().strip()
        self.beneficiary_bank_country = self.beneficiary_bank_country.upper().strip()
        self.swift_reference = self.swift_reference.strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return a short human-readable representation of the SWIFT transfer."""

        return f"swift:{self.transaction_id}:{self.swift_code}"
