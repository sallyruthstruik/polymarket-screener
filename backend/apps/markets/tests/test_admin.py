from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.test import RequestFactory

from apps.markets.admin import PolymarketMarketAdmin
from apps.markets.models import PolymarketMarket
from apps.markets.services.polymarket import (
    PolymarketMarketData,
    PolymarketMarketListFilters,
    PolymarketMarketPage,
    PolymarketMarketRawPayload,
    PolymarketMarketRawPayloadStorageService,
    PolymarketMarketStorageService,
)
from apps.markets.services.prices import PolymarketPriceStorageService


def _build_request(method: str, path: str, data: dict[str, str] | None = None) -> HttpRequest:
    factory = RequestFactory()
    if method == "POST":
        request = factory.post(path, data or {})
    else:
        request = factory.get(path, data or {})
    request.user = cast(Any, SimpleNamespace(is_active=True, is_staff=True))
    request_any = cast(Any, request)
    request_any.session = {}
    request_any._messages = FallbackStorage(request)
    return request


def _market(external_id: str = "1") -> PolymarketMarketData:
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    return PolymarketMarketData(
        external_id=external_id,
        condition_id=f"condition-{external_id}",
        slug=f"market-{external_id}",
        question=f"Market {external_id}",
        description="Description",
        category="sports",
        active=True,
        closed=False,
        archived=False,
        restricted=False,
        accepting_orders=True,
        market_created_at=now,
        market_updated_at=now,
        start_date=now,
        end_date=now,
        liquidity=Decimal("1.0"),
        volume=Decimal("2.0"),
        liquidity_clob=Decimal("3.0"),
        volume_clob=Decimal("4.0"),
        volume_24hr=Decimal("5.0"),
        clob_token_ids=["token-a", "token-b"],
        sync_prices=True,
        first_synced_at=now,
        last_synced_at=now,
        written_at=now,
        row_version=1,
        write_source="sync",
    )


class FakeMarketStorageService(PolymarketMarketStorageService):
    def __init__(self) -> None:
        self.bulk_calls: list[dict[str, object]] = []
        self.saved_market_input: dict[str, object] | None = None

    def list_markets(
        self,
        *,
        filters: PolymarketMarketListFilters,
        page: int,
        page_size: int,
    ) -> PolymarketMarketPage:
        return PolymarketMarketPage(markets=[_market()], total_count=1)

    def get_market(self, external_id: str) -> PolymarketMarketData | None:
        if external_id == "missing":
            return None
        return _market(external_id)

    def set_sync_prices(
        self,
        *,
        external_ids: Sequence[str] | None,
        enabled: bool,
        update_all: bool = False,
    ) -> int:
        self.bulk_calls.append(
            {"external_ids": external_ids, "enabled": enabled, "update_all": update_all}
        )
        return len(external_ids or [])

    def save_admin_edit(
        self,
        *,
        external_id: str,
        market_input: Any,
    ) -> PolymarketMarketData:
        self.saved_market_input = {"external_id": external_id, **market_input.model_dump()}
        return _market(external_id)


class FakePriceStorageService(PolymarketPriceStorageService):
    def __init__(self) -> None:
        return None

    def list_observations(
        self,
        *,
        market_external_id: str | None = None,
        token_id: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        return []

    def build_price_chart(
        self,
        *,
        market_external_id: str,
        token_ids: Sequence[str],
        range_key: str,
    ) -> Any:
        from apps.markets.services.prices import PolymarketPriceChart

        return PolymarketPriceChart(selected_range=range_key, resolved_source=None, series=[])


class FakeRawPayloadStorageService(PolymarketMarketRawPayloadStorageService):
    def __init__(self) -> None:
        return None

    def list_payloads(
        self,
        *,
        market_external_id: str | None = None,
        limit: int = 100,
    ) -> list[PolymarketMarketRawPayload]:
        return [
            PolymarketMarketRawPayload(
                synced_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
                market_external_id=market_external_id or "1",
                condition_id="condition-1",
                slug="market-1",
                payload_json='{"id":"1"}',
            )
        ]


def test_changelist_view_returns_clickhouse_markets(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    storage = FakeMarketStorageService()
    monkeypatch.setattr(admin_instance, "_get_market_storage", lambda: storage)

    response = admin_instance.changelist_view(
        _build_request("GET", "/admin/markets/polymarketmarket/", {"q": "market"})
    )
    assert isinstance(response, TemplateResponse)
    context_data = response.context_data
    assert context_data is not None

    assert context_data["total_count"] == 1
    assert len(context_data["markets"]) == 1


def test_changelist_bulk_action_updates_sync_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    storage = FakeMarketStorageService()
    monkeypatch.setattr(admin_instance, "_get_market_storage", lambda: storage)

    response = admin_instance.changelist_view(
        _build_request(
            "POST",
            "/admin/markets/polymarketmarket/",
            {"action": "enable_sync_prices", "_selected_action": "1"},
        )
    )

    assert storage.bulk_calls == [{"external_ids": ["1"], "enabled": True, "update_all": False}]
    assert response.status_code == 302


def test_change_view_saves_valid_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    storage = FakeMarketStorageService()
    monkeypatch.setattr(admin_instance, "_get_market_storage", lambda: storage)

    response = admin_instance.change_view(
        _build_request(
            "POST",
            "/admin/markets/polymarketmarket/1/change/",
            {
                "condition_id": "condition-1",
                "slug": "market-1",
                "question": "Edited",
                "description": "Edited description",
                "category": "sports",
                "active": "true",
                "closed": "false",
                "archived": "false",
                "restricted": "false",
                "accepting_orders": "true",
                "market_created_at": "2026-07-10 12:00:00",
                "market_updated_at": "2026-07-10 12:00:00",
                "start_date": "2026-07-10 12:00:00",
                "end_date": "2026-07-11 12:00:00",
                "liquidity": "1.0",
                "volume": "2.0",
                "liquidity_clob": "3.0",
                "volume_clob": "4.0",
                "volume_24hr": "5.0",
                "clob_token_ids": '["token-a","token-b"]',
                "sync_prices": "on",
            },
        ),
        "1",
    )

    assert response.status_code == 302
    assert storage.saved_market_input is not None
    assert storage.saved_market_input["question"] == "Edited"


def test_change_view_rejects_invalid_token_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    storage = FakeMarketStorageService()
    monkeypatch.setattr(admin_instance, "_get_market_storage", lambda: storage)

    response = admin_instance.change_view(
        _build_request(
            "POST",
            "/admin/markets/polymarketmarket/1/change/",
            {"clob_token_ids": '{"not":"an array"}'},
        ),
        "1",
    )
    assert isinstance(response, TemplateResponse)
    context_data = response.context_data
    assert context_data is not None

    assert response.status_code == 200
    assert context_data["form"].errors


def test_chart_view_returns_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    monkeypatch.setattr(admin_instance, "_get_market_storage", lambda: FakeMarketStorageService())
    monkeypatch.setattr(admin_instance, "_get_price_storage", lambda: FakePriceStorageService())

    response = admin_instance.chart_view(
        _build_request("GET", "/admin/markets/polymarketmarket/1/chart.svg", {"range": "30d"}),
        "1",
    )

    assert response["Content-Type"] == "image/svg+xml"
    assert "No price history found." in response.content.decode("utf-8")


def test_raw_payloads_view_returns_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_instance = PolymarketMarketAdmin(PolymarketMarket, AdminSite())
    monkeypatch.setattr(
        admin_instance,
        "_get_raw_payload_storage",
        lambda: FakeRawPayloadStorageService(),
    )

    response = admin_instance.raw_payloads_view(
        _build_request(
            "GET",
            "/admin/markets/polymarketmarket/raw-payloads/",
            {"market_external_id": "1", "limit": "25"},
        )
    )
    context_data = response.context_data
    assert context_data is not None

    assert len(context_data["payloads"]) == 1


def test_unmanaged_market_anchor_is_read_only_metadata_only() -> None:
    assert PolymarketMarket._meta.managed is False
    assert PolymarketMarket._meta.db_table == "markets_polymarketmarket"
    assert [field.name for field in PolymarketMarket._meta.fields] == ["external_id"]
