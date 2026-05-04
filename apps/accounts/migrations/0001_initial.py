from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Account",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("iban", models.CharField(db_index=True, max_length=34, unique=True)),
                (
                    "balance",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        max_digits=18,
                    ),
                ),
                (
                    "currency",
                    models.CharField(
                        choices=[("USD", "USD"), ("EUR", "EUR"), ("UAH", "UAH")],
                        max_length=3,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="accounts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                check=models.Q(balance__gte=0),
                name="account_balance_non_negative",
            ),
        ),
    ]
