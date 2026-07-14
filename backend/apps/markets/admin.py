from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.contrib import admin, messages
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html
from pydantic import BaseModel, ConfigDict, ValidationError

from apps.markets.forms import PolymarketMarketAdminForm
from apps.markets.models import PolymarketMarket
from apps.markets.services.polymarket import (
    PolymarketMarketAdminInput,
    PolymarketMarketData,
    PolymarketMarketListFilters,
    PolymarketMarketRawPayloadStorageService,
    PolymarketMarketStorageService,
)
from apps.markets.services.prices import (
    PolymarketPriceChart,
    PolymarketPriceChartSeries,
    PolymarketPriceStorageService,
)

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


class PolymarketMarketAdminFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    search: str = ""
    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    sync_prices: bool | None = None
    page: int = 1
    page_size: int = 50

    def to_storage_filters(self) -> PolymarketMarketListFilters:
        return PolymarketMarketListFilters(
            search=self.search,
            active=self.active,
            closed=self.closed,
            archived=self.archived,
            sync_prices=self.sync_prices,
        )


@admin.register(PolymarketMarket)
class PolymarketMarketAdmin(PolymarketMarketAdminBase):
    change_list_template = "admin/markets/polymarketmarket/change_list.html"
    change_form_template = "admin/markets/polymarketmarket/change_form.html"

    def get_urls(self) -> list[URLPattern]:
        custom_urls = [
            path(
                "",
                self.admin_site.admin_view(self.changelist_view),
                name="markets_polymarketmarket_changelist",
            ),
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
            path(
                "<path:object_id>/chart.svg",
                self.admin_site.admin_view(self.chart_view),
                name="markets_polymarketmarket_chart",
            ),
            path(
                "<path:object_id>/change/",
                self.admin_site.admin_view(self.change_view),
                name="markets_polymarketmarket_change",
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: dict[str, object] | None = None,
    ) -> TemplateResponse | HttpResponseRedirect:
        if request.method == "POST":
            return self._handle_bulk_action(request)

        filters = self._parse_market_filters(request)
        page = self._get_market_storage().list_markets(
            filters=filters.to_storage_filters(),
            page=filters.page,
            page_size=filters.page_size,
        )
        total_pages = max(1, ceil(page.total_count / filters.page_size))
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "model_admin": self,
            "title": "Select Polymarket market to change",
            "filters": filters,
            "markets": page.markets,
            "total_count": page.total_count,
            "page_number": filters.page,
            "page_size": filters.page_size,
            "total_pages": total_pages,
            "has_previous": filters.page > 1,
            "has_next": filters.page < total_pages,
            "query_string_without_page": self._query_string_without_page(request),
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.change_list_template, context)

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, object] | None = None,
    ) -> TemplateResponse | HttpResponseRedirect:
        market = self._get_market_storage().get_market(object_id)
        if market is None:
            raise Http404("Market not found")

        if request.method == "POST":
            form = PolymarketMarketAdminForm(request.POST)
            if form.is_valid():
                try:
                    market_input = PolymarketMarketAdminInput.model_validate(form.cleaned_data)
                except ValidationError as error:
                    form.add_error(None, error.errors()[0]["msg"])
                else:
                    self._get_market_storage().save_admin_edit(
                        external_id=object_id,
                        market_input=market_input,
                    )
                    self.message_user(request, "Market changes saved.", level=messages.SUCCESS)
                    return HttpResponseRedirect(request.path)
        else:
            form = PolymarketMarketAdminForm(initial=self._build_form_initial(market))

        selected_range = self._parse_chart_range(request)
        chart_url = (
            f"{reverse('admin:markets_polymarketmarket_chart', args=[object_id])}"
            f"?{urlencode({'range': selected_range})}"
        )
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Change Polymarket market {market.external_id}",
            "original": market,
            "market": market,
            "form": form,
            "chart_url": chart_url,
            "selected_range": selected_range,
            "chart_ranges": ("24h", "7d", "30d", "all"),
            "polymarket_url": self._build_polymarket_market_url(market),
            "prices_url": self._build_prices_url(market.external_id),
            "raw_payloads_url": self._build_raw_payloads_url(market.external_id),
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.change_form_template, context)

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

    def chart_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        market = self._get_market_storage().get_market(object_id)
        if market is None:
            raise Http404("Market not found")
        chart = self._get_price_storage().build_price_chart(
            market_external_id=market.external_id,
            token_ids=market.clob_token_ids,
            range_key=self._parse_chart_range(request),
        )
        svg = self._render_chart_svg(chart)
        return HttpResponse(svg, content_type="image/svg+xml")

    def clickhouse_rows(self, market: PolymarketMarketData) -> str:
        return format_html(
            '<a href="{}">Prices</a> | <a href="{}">Raw payloads</a>',
            self._build_prices_url(market.external_id),
            self._build_raw_payloads_url(market.external_id),
        )

    def _handle_bulk_action(self, request: HttpRequest) -> HttpResponseRedirect:
        action = request.POST.get("action", "")
        selected_ids = [value for value in request.POST.getlist("_selected_action") if value != ""]
        if not selected_ids:
            self.message_user(request, "Select at least one market.", level=messages.WARNING)
            return HttpResponseRedirect(request.path)
        if action == "enable_sync_prices":
            updated_count = self._get_market_storage().set_sync_prices(
                external_ids=selected_ids,
                enabled=True,
            )
            self.message_user(
                request,
                f"Enabled price sync for {updated_count} markets.",
                level=messages.SUCCESS,
            )
        elif action == "disable_sync_prices":
            updated_count = self._get_market_storage().set_sync_prices(
                external_ids=selected_ids,
                enabled=False,
            )
            self.message_user(
                request,
                f"Disabled price sync for {updated_count} markets.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(request, "Unknown action.", level=messages.ERROR)
        redirect_url = request.path
        if request.META.get("QUERY_STRING"):
            redirect_url = f"{redirect_url}?{request.META['QUERY_STRING']}"
        return HttpResponseRedirect(redirect_url)

    def _build_form_initial(self, market: PolymarketMarketData) -> dict[str, object]:
        return {
            "condition_id": market.condition_id,
            "slug": market.slug,
            "question": market.question,
            "description": market.description,
            "category": market.category,
            "active": self._bool_to_choice(market.active),
            "closed": self._bool_to_choice(market.closed),
            "archived": self._bool_to_choice(market.archived),
            "restricted": self._bool_to_choice(market.restricted),
            "accepting_orders": self._bool_to_choice(market.accepting_orders),
            "market_created_at": market.market_created_at,
            "market_updated_at": market.market_updated_at,
            "start_date": market.start_date,
            "end_date": market.end_date,
            "liquidity": market.liquidity,
            "volume": market.volume,
            "liquidity_clob": market.liquidity_clob,
            "volume_clob": market.volume_clob,
            "volume_24hr": market.volume_24hr,
            "clob_token_ids": self._format_token_ids(market.clob_token_ids),
            "sync_prices": market.sync_prices,
        }

    def _parse_market_filters(self, request: HttpRequest) -> PolymarketMarketAdminFilters:
        return PolymarketMarketAdminFilters(
            search=request.GET.get("q", "").strip(),
            active=self._parse_optional_bool(request.GET.get("active")),
            closed=self._parse_optional_bool(request.GET.get("closed")),
            archived=self._parse_optional_bool(request.GET.get("archived")),
            sync_prices=self._parse_optional_bool(request.GET.get("sync_prices")),
            page=self._parse_page(request.GET.get("p")),
            page_size=self._parse_page_size(request.GET.get("page_size")),
        )

    def _parse_price_filters(self, request: HttpRequest) -> PolymarketPriceInspectorFilters:
        return PolymarketPriceInspectorFilters(
            market_external_id=request.GET.get("market_external_id", "").strip(),
            token_id=request.GET.get("token_id", "").strip(),
            limit=self._parse_limit(request),
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
        return min(max(self._parse_positive_int(request.GET.get("limit"), default=100), 1), 500)

    def _parse_page(self, value: str | None) -> int:
        return max(self._parse_positive_int(value, default=1), 1)

    def _parse_page_size(self, value: str | None) -> int:
        return min(max(self._parse_positive_int(value, default=50), 10), 200)

    def _parse_positive_int(self, value: str | None, *, default: int) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _parse_optional_bool(self, value: str | None) -> bool | None:
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    def _parse_chart_range(self, request: HttpRequest) -> str:
        range_key = request.GET.get("range", "30d")
        if range_key in {"24h", "7d", "30d", "all"}:
            return range_key
        return "30d"

    def _query_string_without_page(self, request: HttpRequest) -> str:
        query = request.GET.copy()
        query.pop("p", None)
        return query.urlencode()

    def _format_token_ids(self, token_ids: list[str]) -> str:
        return json.dumps(token_ids)

    def _bool_to_choice(self, value: bool | None) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return ""

    def _build_prices_url(self, external_id: str) -> str:
        query = urlencode({"market_external_id": external_id})
        return f"{reverse('admin:markets_polymarketmarket_prices')}?{query}"

    def _build_raw_payloads_url(self, external_id: str) -> str:
        query = urlencode({"market_external_id": external_id})
        return f"{reverse('admin:markets_polymarketmarket_raw_payloads')}?{query}"

    def _build_polymarket_market_url(self, market: PolymarketMarketData) -> str | None:
        if market.slug == "":
            return None
        return f"https://polymarket.com/event/{market.slug}"

    def _get_market_storage(self) -> PolymarketMarketStorageService:
        return PolymarketMarketStorageService()

    def _get_price_storage(self) -> PolymarketPriceStorageService:
        return PolymarketPriceStorageService()

    def _get_raw_payload_storage(self) -> PolymarketMarketRawPayloadStorageService:
        return PolymarketMarketRawPayloadStorageService()

    def _render_chart_svg(self, chart: PolymarketPriceChart) -> str:
        width = 960
        height = 320
        padding_left = 64
        padding_right = 24
        padding_top = 24
        padding_bottom = 36
        plot_width = width - padding_left - padding_right
        plot_height = height - padding_top - padding_bottom
        if not chart.series:
            return self._render_empty_chart_svg(width=width, height=height)

        timestamps = [
            observation.observed_at
            for series in chart.series
            for observation in series.observations
        ]
        min_timestamp = min(timestamps)
        max_timestamp = max(timestamps)
        if min_timestamp == max_timestamp:
            max_timestamp = min_timestamp + timedelta(microseconds=1)

        colors = ("#2563eb", "#dc2626", "#059669", "#7c3aed")
        paths: list[str] = []
        labels: list[str] = []
        for index, series in enumerate(chart.series):
            color = colors[index % len(colors)]
            path_data = self._build_svg_path(
                series,
                min_timestamp,
                max_timestamp,
                padding_left,
                padding_top,
                plot_width,
                plot_height,
            )
            paths.append(
                f'<path d="{path_data}" fill="none" stroke="{color}" ' 'stroke-width="2" />'
            )
            labels.append(
                f'<text x="{padding_left + (index * 140)}" y="16" '
                f'fill="{color}" font-size="12">{series.token_id}</text>'
            )

        grid_lines = []
        for row_index in range(6):
            y_value = padding_top + (plot_height / 5) * row_index
            price_value = Decimal("1") - (Decimal(row_index) / Decimal(5))
            grid_lines.append(
                f'<line x1="{padding_left}" y1="{y_value:.2f}" '
                f'x2="{width - padding_right}" y2="{y_value:.2f}" '
                'stroke="#e5e7eb" stroke-width="1" />'
            )
            grid_lines.append(
                f'<text x="12" y="{y_value + 4:.2f}" '
                f'fill="#4b5563" font-size="12">{price_value:.1f}</text>'
            )

        min_label = min_timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        max_label = max_timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        axis_labels = [
            f'<text x="{padding_left}" y="{height - 10}" fill="#4b5563" '
            f'font-size="12">{min_label}</text>',
            f'<text x="{width - padding_right - 180}" y="{height - 10}" '
            f'fill="#4b5563" font-size="12">{max_label}</text>',
        ]
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="Polymarket price chart">'
            '<rect width="100%" height="100%" fill="white" />'
            f"{''.join(grid_lines)}"
            f"{''.join(paths)}"
            f"{''.join(labels)}"
            f"{''.join(axis_labels)}"
            "</svg>"
        )

    def _build_svg_path(
        self,
        series: PolymarketPriceChartSeries,
        min_timestamp: datetime,
        max_timestamp: datetime,
        padding_left: int,
        padding_top: int,
        plot_width: int,
        plot_height: int,
    ) -> str:
        total_seconds = max((max_timestamp - min_timestamp).total_seconds(), 1)
        commands: list[str] = []
        for observation in series.observations:
            x_ratio = (observation.observed_at - min_timestamp).total_seconds() / total_seconds
            y_ratio = float(max(min(observation.price, Decimal("1")), Decimal("0")))
            x = padding_left + (plot_width * x_ratio)
            y = padding_top + (plot_height * (1 - y_ratio))
            command = "M" if not commands else "L"
            commands.append(f"{command} {x:.2f} {y:.2f}")
        return " ".join(commands)

    def _render_empty_chart_svg(self, *, width: int, height: int) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="No price history">'
            '<rect width="100%" height="100%" fill="white" />'
            '<text x="50%" y="50%" text-anchor="middle" fill="#4b5563" font-size="16">'
            "No price history found."
            "</text>"
            "</svg>"
        )
