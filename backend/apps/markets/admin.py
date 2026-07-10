from typing import TYPE_CHECKING

from django.contrib import admin

from apps.markets.models import PolymarketMarket

if TYPE_CHECKING:
    PolymarketMarketAdminBase = admin.ModelAdmin[PolymarketMarket]
else:
    PolymarketMarketAdminBase = admin.ModelAdmin


@admin.register(PolymarketMarket)
class PolymarketMarketAdmin(PolymarketMarketAdminBase):
    list_display = (
        "external_id",
        "question",
        "slug",
        "active",
        "closed",
        "accepting_orders",
        "market_created_at",
        "volume",
        "liquidity",
    )
    list_filter = ("active", "closed", "archived", "restricted", "accepting_orders")
    search_fields = ("external_id", "condition_id", "slug", "question")
    readonly_fields = ("first_synced_at", "last_synced_at", "raw_payload")
    ordering = ("-market_created_at", "-external_id")
    date_hierarchy = "market_created_at"
