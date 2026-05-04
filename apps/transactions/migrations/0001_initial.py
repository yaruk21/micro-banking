from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Transaction",
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
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                (
                    "status",
                    models.CharField(
                        choices=[("success", "Success"), ("failed", "Failed")],
                        max_length=7,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("failure_reason", models.TextField(blank=True)),
                (
                    "from_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outgoing_transactions",
                        to="accounts.account",
                    ),
                ),
                (
                    "to_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="incoming_transactions",
                        to="accounts.account",
                    ),
                ),
            ],
            options={"ordering": ("-created_at", "-id")},
        ),
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.CheckConstraint(
                check=models.Q(amount__gt=Decimal("0.00")),
                name="transaction_amount_positive",
            ),
        ),
    ]
