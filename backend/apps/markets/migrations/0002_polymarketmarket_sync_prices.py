from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("markets", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="polymarketmarket",
            name="sync_prices",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddIndex(
            model_name="polymarketmarket",
            index=models.Index(
                fields=["sync_prices", "closed"],
                name="markets_pol_sync_pr_5cc962_idx",
            ),
        ),
    ]
