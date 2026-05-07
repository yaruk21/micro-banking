from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0014_transactionchallenge"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TransactionReport",
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
                ("date_from", models.DateField()),
                ("date_to", models.DateField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("file_name", models.CharField(blank=True, max_length=255)),
                (
                    "content_type",
                    models.CharField(
                        blank=True,
                        default="application/pdf",
                        max_length=100,
                    ),
                ),
                ("pdf_content", models.BinaryField(blank=True, null=True)),
                ("failure_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("processing_started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transaction_reports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="transactionreport",
            index=models.Index(
                fields=("user", "status", "created_at"),
                name="txn_report_user_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="transactionreport",
            constraint=models.CheckConstraint(
                check=models.Q(("date_to__gte", models.F("date_from"))),
                name="txn_report_date_range_valid",
            ),
        ),
    ]
