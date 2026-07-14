from typing import TYPE_CHECKING

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.urls import URLPattern, path
from django.utils.html import format_html
from pydantic import BaseModel, ConfigDict

from apps.markets.models import PolymarketMarket
from apps.markets.services.polymarket import PolymarketMarketRawPayloadStorageService
from apps.markets.services.prices import PolymarketPriceStorageService

if TYPE_CHECKING:
    PolymarketMarketAdminBase = admin.ModelAdmin[PolymarketMarket]
else:
    PolymarketMarketAdminBase = admin.ModelAdmin


class PolymarketPriceInspectorFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_external_id: str
    token_id: str
    limit: int


class PolymarketRawPayloadInspectorFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_external_id: str
    limit: int


@admin.register(PolymarketMarket)
class PolymarketMarketAdmin(PolymarketMarketAdminBase):
    list_display = (
        "external_id",
        "question",
        "slug",
        "polymarket_link",
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
    readonly_fields = ("polymarket_link", "first_synced_at", "last_synced_at")
    ordering = ("-market_created_at", "-external_id")
    date_hierarchy = "market_created_at"
    actions = ("enable_sync_prices", "disable_sync_prices")
    change_list_template = "admin/markets/polymarketmarket/change_list.html"

    def get_urls(self) -> list[URLPattern]:
        custom_urls = [
            path(
                "prices/",
                self.admin_site.admin_view(self.prices_view),
                name="markets_polymarketmarket_prices",
            ),
            path(
                "raw-payloads/",
                self.admin_site.admin_view(self.raw_payloads_view),
                name="markets_polymarketmarket_raw_payloads",
            ),
        ]
        return custom_urls + super().get_urls()

    def prices_view(self, request: HttpRequest) -> TemplateResponse:
        filters = self._parse_price_filters(request)
        observations = self._get_price_storage().list_observations(
            market_external_id=filters.market_external_id or None,
            token_id=filters.token_id or None,
            limit=filters.limit,
        )
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Polymarket Prices",
            "filters": filters,
            "observations": observations,
        }
        return TemplateResponse(
            request,
            "admin/markets/polymarketmarket/prices.html",
            context,
        )

    def raw_payloads_view(self, request: HttpRequest) -> TemplateResponse:
        filters = self._parse_raw_payload_filters(request)
        payloads = self._get_raw_payload_storage().list_payloads(
            market_external_id=filters.market_external_id or None,
            limit=filters.limit,
        )
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Polymarket Raw Payloads",
            "filters": filters,
            "payloads": payloads,
        }
        return TemplateResponse(
            request,
            "admin/markets/polymarketmarket/raw_payloads.html",
            context,
        )

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

    @admin.display(description="Polymarket")
    def polymarket_link(self, obj: PolymarketMarket) -> str:
        if obj.slug == "":
            return "-"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">Open market</a>',
            self._build_polymarket_market_url(obj),
        )

    def _get_price_storage(self) -> PolymarketPriceStorageService:
        return PolymarketPriceStorageService()

    def _get_raw_payload_storage(self) -> PolymarketMarketRawPayloadStorageService:
        return PolymarketMarketRawPayloadStorageService()

    def _parse_price_filters(self, request: HttpRequest) -> PolymarketPriceInspectorFilters:
        bounded_limit = self._parse_limit(request)
        return PolymarketPriceInspectorFilters(
            market_external_id=request.GET.get("market_external_id", "").strip(),
            token_id=request.GET.get("token_id", "").strip(),
            limit=bounded_limit,
        )

    def _parse_raw_payload_filters(
        self,
        request: HttpRequest,
    ) -> PolymarketRawPayloadInspectorFilters:
        return PolymarketRawPayloadInspectorFilters(
            market_external_id=request.GET.get("market_external_id", "").strip(),
            limit=self._parse_limit(request),
        )

    def _parse_limit(self, request: HttpRequest) -> int:
        raw_limit = request.GET.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 100
        return min(max(limit, 1), 500)

    def _build_polymarket_market_url(self, obj: PolymarketMarket) -> str:
        return f"https://polymarket.com/event/{obj.slug}"
