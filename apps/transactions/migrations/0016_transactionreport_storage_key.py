from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0015_transactionreport"),
    ]

    operations = [
        migrations.AddField(
            model_name="transactionreport",
            name="storage_key",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
