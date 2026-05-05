from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("transactions", "0004_transactionoutbox"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransactionBatch",
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
                ("idempotency_key", models.CharField(max_length=255)),
                ("request_fingerprint", models.CharField(max_length=255)),
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
                        max_length=10,
                    ),
                ),
                ("total_items", models.PositiveIntegerField(default=0)),
                ("processed_items", models.PositiveIntegerField(default=0)),
                ("succeeded_items", models.PositiveIntegerField(default=0)),
                ("failed_items", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("processing_started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("failure_reason", models.TextField(blank=True)),
                (
                    "initiated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transaction_batches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="transactionbatch",
            constraint=models.UniqueConstraint(
                fields=("initiated_by", "idempotency_key"),
                name="txn_batch_user_idem_uniq",
            ),
        ),
        migrations.CreateModel(
            name="TransactionBatchItem",
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
                ("sequence", models.PositiveIntegerField()),
                ("from_account_iban", models.CharField(max_length=34)),
                ("to_account_iban", models.CharField(max_length=34)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("idempotency_key", models.CharField(max_length=255)),
                ("created_transaction", models.BooleanField(default=False)),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="transactions.transactionbatch",
                    ),
                ),
                (
                    "transaction",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="batch_items",
                        to="transactions.transaction",
                    ),
                ),
            ],
            options={
                "ordering": ("sequence", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="transactionbatchitem",
            constraint=models.UniqueConstraint(
                fields=("batch", "sequence"),
                name="txn_batch_item_seq_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="transactionbatchitem",
            index=models.Index(
                fields=("batch", "sequence"),
                name="txn_batch_item_seq_idx",
            ),
        ),
    ]
