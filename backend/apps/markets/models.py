from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.db import models

from apps.markets.types import JsonList


class PolymarketMarket(models.Model):
    external_id: models.CharField[str, str] = models.CharField(max_length=64, unique=True)
    condition_id: models.CharField[str, str] = models.CharField(max_length=128, db_index=True)
    slug: models.CharField[str, str] = models.CharField(max_length=512, blank=True, db_index=True)
    question: models.TextField[str, str] = models.TextField(blank=True)
    description: models.TextField[str, str] = models.TextField(blank=True)
    category: models.CharField[str, str] = models.CharField(max_length=255, blank=True)

    active: models.BooleanField[bool | None, bool | None] = models.BooleanField(
        null=True, db_index=True
    )
    closed: models.BooleanField[bool | None, bool | None] = models.BooleanField(
        null=True, db_index=True
    )
    archived: models.BooleanField[bool | None, bool | None] = models.BooleanField(
        null=True, db_index=True
    )
    restricted: models.BooleanField[bool | None, bool | None] = models.BooleanField(
        null=True, db_index=True
    )
    accepting_orders: models.BooleanField[bool | None, bool | None] = models.BooleanField(
        null=True, db_index=True
    )

    market_created_at: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(null=True, blank=True, db_index=True)
    )
    market_updated_at: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(null=True, blank=True, db_index=True)
    )
    start_date: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True, db_index=True
    )
    end_date: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True, db_index=True
    )

    liquidity: models.DecimalField[Decimal | None, Decimal | None] = models.DecimalField(
        max_digits=32,
        decimal_places=12,
        null=True,
        blank=True,
        db_index=True,
    )
    volume: models.DecimalField[Decimal | None, Decimal | None] = models.DecimalField(
        max_digits=32,
        decimal_places=12,
        null=True,
        blank=True,
        db_index=True,
    )
    liquidity_clob: models.DecimalField[Decimal | None, Decimal | None] = models.DecimalField(
        max_digits=32, decimal_places=12, null=True, blank=True
    )
    volume_clob: models.DecimalField[Decimal | None, Decimal | None] = models.DecimalField(
        max_digits=32, decimal_places=12, null=True, blank=True
    )
    volume_24hr: models.DecimalField[Decimal | None, Decimal | None] = models.DecimalField(
        max_digits=32, decimal_places=12, null=True, blank=True
    )

    clob_token_ids: models.JSONField[JsonList, JsonList] = models.JSONField(
        default=list, blank=True
    )
    sync_prices: models.BooleanField[bool, bool] = models.BooleanField(
        default=False, db_index=True
    )
    first_synced_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True
    )
    last_synced_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-market_created_at", "-external_id"]
        indexes = [
            models.Index(fields=["closed", "-market_created_at"]),
            models.Index(fields=["active", "closed"]),
            models.Index(fields=["sync_prices", "closed"]),
        ]

    def __str__(self) -> str:
        return self.question or self.slug or self.external_id

    @property
    def liquidity_display(self) -> Decimal | None:
        return self.liquidity
