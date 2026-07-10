from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest
from django.db.models import QuerySet

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
        "sync_prices",
        "market_created_at",
        "volume",
        "liquidity",
    )
    list_filter = ("sync_prices", "active", "closed", "archived", "restricted", "accepting_orders")
    search_fields = ("external_id", "condition_id", "slug", "question")
    readonly_fields = ("first_synced_at", "last_synced_at", "raw_payload")
    ordering = ("-market_created_at", "-external_id")
    date_hierarchy = "market_created_at"
    actions = ("enable_sync_prices", "disable_sync_prices")

    @admin.action(description="Enable price sync for selected markets")
    def enable_sync_prices(
        self,
        request: HttpRequest,
        queryset: QuerySet[PolymarketMarket],
    ) -> None:
        updated_count = queryset.update(sync_prices=True)
        self.message_user(request, f"Enabled price sync for {updated_count} markets.")

    @admin.action(description="Disable price sync for selected markets")
    def disable_sync_prices(
        self,
        request: HttpRequest,
        queryset: QuerySet[PolymarketMarket],
    ) -> None:
        updated_count = queryset.update(sync_prices=False)
        self.message_user(request, f"Disabled price sync for {updated_count} markets.")
