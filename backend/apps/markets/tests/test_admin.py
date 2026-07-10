from typing import Any, cast

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest
from django.test import RequestFactory

from apps.markets.admin import PolymarketMarketAdmin
from apps.markets.models import PolymarketMarket


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
