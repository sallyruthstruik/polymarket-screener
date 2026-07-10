from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.test import RequestFactory

from apps.markets.admin import PolymarketMarketAdmin
from apps.markets.models import PolymarketMarket
from apps.markets.services.prices import (
    PolymarketPriceInspectionRow,
    PolymarketPriceStorageService,
)


def _build_request() -> HttpRequest:
    request = RequestFactory().post("/admin/")
    request_any = cast(Any, request)
    request_any.session = {}
    request_any._messages = FallbackStorage(request)
    return request


def _create_market(*, external_id: str, sync_prices: bool) -> PolymarketMarket:
    return PolymarketMarket.objects.create(
        external_id=external_id,
        condition_id=f"condition-{external_id}",
        slug=f"market-{external_id}",
        question=f"Market {external_id}",
        active=True,
        closed=False,
        archived=False,
        restricted=False,
        accepting_orders=True,
        clob_token_ids=[f"token-{external_id}-yes", f"token-{external_id}-no"],
        sync_prices=sync_prices,
        raw_payload={},
    )


def test_admin_action_enables_sync_prices(db: None) -> None:
    market = _create_market(external_id="1", sync_prices=False)
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())

    admin_instance.enable_sync_prices(
        _build_request(),
        PolymarketMarket.objects.filter(pk=market.pk),
    )

    market.refresh_from_db()
    assert market.sync_prices is True


def test_admin_action_disables_sync_prices(db: None) -> None:
    market = _create_market(external_id="1", sync_prices=True)
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())

    admin_instance.disable_sync_prices(
        _build_request(),
        PolymarketMarket.objects.filter(pk=market.pk),
    )

    market.refresh_from_db()
    assert market.sync_prices is False


def test_prices_view_returns_observations(monkeypatch: Any) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())

    class FakePriceStorageService(PolymarketPriceStorageService):
        def list_observations(
            self,
            *,
            market_external_id: str | None = None,
            token_id: str | None = None,
            limit: int = 100,
        ) -> list[PolymarketPriceInspectionRow]:
            assert market_external_id == "1"
            assert token_id == "token-1"
            assert limit == 25
            return [
                PolymarketPriceInspectionRow(
                    observed_at=_create_market_timestamp(),
                    market_external_id="1",
                    condition_id="condition-1",
                    token_id="token-1",
                    side="BUY",
                    price=Decimal("0.51"),
                    source="clob_prices",
                )
            ]

    monkeypatch.setattr(
        admin_instance,
        "_get_price_storage",
        lambda: FakePriceStorageService(),
    )
    request = RequestFactory().get(
        "/admin/markets/polymarketmarket/prices/",
        {"market_external_id": "1", "token_id": "token-1", "limit": "25"},
    )
    request.user = cast(Any, SimpleNamespace(is_active=True, is_staff=True))

    response = admin_instance.prices_view(request)
    context_data = response.context_data
    assert context_data is not None

    assert isinstance(response, TemplateResponse)
    assert response.template_name == "admin/markets/polymarketmarket/prices.html"
    assert len(context_data["observations"]) == 1
    assert context_data["filters"].limit == 25


def test_prices_view_bounds_invalid_limit() -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    request = RequestFactory().get(
        "/admin/markets/polymarketmarket/prices/",
        {"limit": "9999"},
    )

    filters = admin_instance._parse_price_filters(request)

    assert filters.limit == 500


def _create_market_timestamp() -> datetime:
    return datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
