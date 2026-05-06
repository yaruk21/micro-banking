from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


# Copies existing idempotency metadata into the standalone registry table.
def populate_transaction_idempotency_registry(apps, schema_editor):
    """Backfill the new idempotency registry from existing transaction rows."""

    Transaction = apps.get_model("transactions", "Transaction")
    TransactionIdempotencyKey = apps.get_model(
        "transactions",
        "TransactionIdempotencyKey",
    )

    batch = []
    for transaction in Transaction.objects.iterator():
        batch.append(
            TransactionIdempotencyKey(
                initiated_by_id=transaction.initiated_by_id,
                idempotency_key=transaction.idempotency_key,
                request_fingerprint=transaction.request_fingerprint,
                transaction_id=transaction.id,
            )
        )
        if len(batch) >= 1000:
            TransactionIdempotencyKey.objects.bulk_create(batch)
            batch = []
    if batch:
        TransactionIdempotencyKey.objects.bulk_create(batch)


# Clears the standalone idempotency registry during migration rollback.
def clear_transaction_idempotency_registry(apps, schema_editor):
    """Remove all registry rows created by the forward backfill."""

    TransactionIdempotencyKey = apps.get_model(
        "transactions",
        "TransactionIdempotencyKey",
    )
    TransactionIdempotencyKey.objects.all().delete()


# Introduces the standalone idempotency registry needed before table partitioning.
class Migration(migrations.Migration):
    """Prepare transaction idempotency and references for monthly partitioning."""

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("transactions", "0008_transaction_status_time_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransactionIdempotencyKey",
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
                ("transaction_id", models.BigIntegerField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "initiated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transaction_idempotency_keys",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("id",),
            },
        ),
        migrations.RunPython(
            populate_transaction_idempotency_registry,
            reverse_code=clear_transaction_idempotency_registry,
        ),
        migrations.AddConstraint(
            model_name="transactionidempotencykey",
            constraint=models.UniqueConstraint(
                fields=("initiated_by", "idempotency_key"),
                name="txn_idem_registry_user_key_uniq",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="transaction",
            name="transaction_initiated_by_idempotency_key_uniq",
        ),
        migrations.AlterField(
            model_name="transaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True),
        ),
        migrations.AlterField(
            model_name="transactionbatchitem",
            name="transaction",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="batch_items",
                to="transactions.transaction",
            ),
        ),
        migrations.AlterField(
            model_name="transactionoutbox",
            name="transaction",
            field=models.OneToOneField(
                db_constraint=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="outbox",
                to="transactions.transaction",
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=("id",),
                name="txn_id_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=("status",),
                name="txn_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=("created_at",),
                name="txn_created_at_idx",
            ),
        ),
    ]
