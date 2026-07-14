from __future__ import annotations

from django.db import models


class PolymarketMarket(models.Model):
    external_id: models.CharField[str, str] = models.CharField(max_length=64, primary_key=True)

    class Meta:
        managed = False
        verbose_name = "Polymarket market"
        verbose_name_plural = "Polymarket markets"
        db_table = "markets_polymarketmarket"

    def __str__(self) -> str:
        return self.external_id
