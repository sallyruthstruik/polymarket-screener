from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

import pytest

from apps.markets.client import PolymarketGammaClient, PolymarketGammaMarket
from apps.markets.models import PolymarketMarket
from apps.markets.services import PolymarketMarketStorageService, PolymarketMarketSyncService
from apps.markets.types import JsonObject


def _market_payload(
    *,
    external_id: str,
    question: str,
    created_at: str = "2026-07-10T10:27:07.728726Z",
    closed: bool = False,
) -> JsonObject:
    return {
        "id": external_id,
        "conditionId": f"condition-{external_id}",
        "slug": f"market-{external_id}",
        "question": question,
        "description": "Market description",
        "active": not closed,
        "closed": closed,
        "archived": False,
        "restricted": True,
        "acceptingOrders": not closed,
        "createdAt": created_at,
        "updatedAt": "2026-07-10T10:30:29.777402Z",
        "startDate": "2026-07-10T10:28:06.832035Z",
        "endDate": "2026-07-11T10:25:00Z",
        "liquidityNum": 1407.3168,
        "volumeNum": "200.857821",
        "liquidityClob": 1407.3168,
        "volumeClob": "200.857821",
        "volume24hr": "100.25",
        "clobTokenIds": '["token-a", "token-b"]',
    }


def _gamma_market(payload: JsonObject) -> PolymarketGammaMarket:
    return PolymarketGammaMarket(
        external_id=str(payload["id"]),
        created_at=datetime.fromisoformat(str(payload["createdAt"]).replace("Z", "+00:00")),
        payload=payload,
    )


@pytest.mark.django_db
def test_storage_service_upserts_market() -> None:
    storage = PolymarketMarketStorageService()
    first_payload = _market_payload(external_id="2869150", question="First question")
    second_payload = _market_payload(external_id="2869150", question="Updated question")

    first_result = storage.upsert_market(_gamma_market(first_payload))
    second_result = storage.upsert_market(_gamma_market(second_payload))

    assert first_result.created is True
    assert second_result.created is False
    assert second_result.market.question == "Updated question"
    assert second_result.market.liquidity == Decimal("1407.3168")
    assert PolymarketMarket.objects.count() == 1


class FakeGammaClient(PolymarketGammaClient):
    def __init__(self) -> None:
        self.closed_filters: list[bool] = []

    def iter_markets(
        self,
        *,
        closed: bool,
        created_since: datetime | None = None,
        page_size: int = 500,
        max_markets: int | None = None,
    ) -> Iterator[PolymarketGammaMarket]:
        self.closed_filters.append(closed)
        payloads = [
            _market_payload(external_id="1", question="Open market", closed=False),
            _market_payload(external_id="2", question="Closed market", closed=True),
        ]
        for payload in payloads[:max_markets]:
            yield _gamma_market(payload)


@pytest.mark.django_db
def test_sync_service_includes_closed_markets_when_requested() -> None:
    client = FakeGammaClient()
    service = PolymarketMarketSyncService(client=client)

    result = service.sync_markets(include_closed=True, page_size=2, max_markets=3)

    assert client.closed_filters == [False, True]
    assert result.fetched_count == 3
    assert result.created_count == 2
    assert result.updated_count == 1


@pytest.mark.django_db
def test_sync_service_defaults_to_open_markets() -> None:
    client = FakeGammaClient()
    service = PolymarketMarketSyncService(client=client)

    service.sync_markets(include_closed=False, page_size=2, max_markets=1)

    assert client.closed_filters == [False]
