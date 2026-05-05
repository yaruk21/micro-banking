from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def populate_transaction_idempotency_fields(apps, schema_editor):
    Transaction = apps.get_model("transactions", "Transaction")

    for transaction in Transaction.objects.select_related("from_account").iterator():
        transaction.initiated_by_id = transaction.from_account.owner_id
        transaction.idempotency_key = f"legacy-{transaction.id}"
        transaction.request_fingerprint = f"legacy-{transaction.id}"
        transaction.save(
            update_fields=[
                "initiated_by",
                "idempotency_key",
                "request_fingerprint",
            ]
        )


def clear_transaction_idempotency_fields(apps, schema_editor):
    Transaction = apps.get_model("transactions", "Transaction")
    Transaction.objects.update(
        initiated_by_id=None,
        idempotency_key=None,
        request_fingerprint="",
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("transactions", "0002_transaction_async_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="idempotency_key",
            field=models.CharField(max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="transaction",
            name="initiated_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="initiated_transactions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="request_fingerprint",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(
            populate_transaction_idempotency_fields,
            reverse_code=clear_transaction_idempotency_fields,
        ),
        migrations.AlterField(
            model_name="transaction",
            name="idempotency_key",
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="initiated_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="initiated_transactions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="request_fingerprint",
            field=models.CharField(max_length=255),
        ),
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.UniqueConstraint(
                fields=("initiated_by", "idempotency_key"),
                name="transaction_initiated_by_idempotency_key_uniq",
            ),
        ),
    ]
