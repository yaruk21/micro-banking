from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0003_transaction_idempotency_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransactionOutbox",
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
                ("correlation_id", models.CharField(blank=True, max_length=255)),
                ("delivery_attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.TextField(blank=True)),
                ("celery_task_id", models.CharField(blank=True, max_length=255)),
                (
                    "published_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "transaction",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="outbox",
                        to="transactions.transaction",
                    ),
                ),
            ],
            options={
                "ordering": ("created_at", "id"),
            },
        ),
        migrations.AddIndex(
            model_name="transactionoutbox",
            index=models.Index(
                fields=("published_at", "created_at"),
                name="transaction_outbox_publish_idx",
            ),
        ),
    ]
