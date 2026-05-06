from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0007_transaction_fee_amount_transaction_fee_currency"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=["status", "created_at"],
                name="txn_status_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=["status", "processing_started_at"],
                name="txn_status_proc_started_idx",
            ),
        ),
    ]
