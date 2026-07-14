from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("markets", "0003_remove_polymarketmarket_raw_payload"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="DROP TABLE IF EXISTS markets_polymarketmarket CASCADE",
                    reverse_sql=migrations.RunSQL.noop,
                )
            ],
            state_operations=[
                migrations.DeleteModel(name="PolymarketMarket"),
                migrations.CreateModel(
                    name="PolymarketMarket",
                    fields=[
                        (
                            "external_id",
                            models.CharField(max_length=64, primary_key=True, serialize=False),
                        )
                    ],
                    options={
                        "managed": False,
                        "verbose_name": "Polymarket market",
                        "verbose_name_plural": "Polymarket markets",
                        "db_table": "markets_polymarketmarket",
                    },
                ),
            ],
        ),
    ]
