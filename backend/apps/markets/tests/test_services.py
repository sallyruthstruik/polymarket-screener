import json
from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal

import pytest
from pytest import CaptureFixture

from apps.markets.clients.polymarket import PolymarketGammaClient, PolymarketGammaMarket
from apps.markets.models import PolymarketMarket
from apps.markets.services.clickhouse import ClickHouseClient
from apps.markets.services.polymarket import (
    PolymarketMarketRawPayloadStorageService,
    PolymarketMarketStorageService,
    PolymarketMarketSyncService,
)
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
    assert not hasattr(PolymarketMarket.objects.get(), "raw_payload")


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
    raw_payload_storage = FakeRawPayloadStorageService()
    service = PolymarketMarketSyncService(client=client, raw_payload_storage=raw_payload_storage)

    result = service.sync_markets(include_closed=True, page_size=2, max_markets=3)

    assert client.closed_filters == [False, True]
    assert raw_payload_storage.table_ensured is True
    assert len(raw_payload_storage.markets) == 3
    assert result.fetched_count == 3
    assert result.created_count == 2
    assert result.updated_count == 1


@pytest.mark.django_db
def test_sync_service_defaults_to_open_markets() -> None:
    client = FakeGammaClient()
    service = PolymarketMarketSyncService(
        client=client,
        raw_payload_storage=FakeRawPayloadStorageService(),
    )

    service.sync_markets(include_closed=False, page_size=2, max_markets=1)

    assert client.closed_filters == [False]


@pytest.mark.django_db
def test_market_sync_service_logs_progress(capsys: CaptureFixture[str]) -> None:
    client = FakeGammaClient()
    service = PolymarketMarketSyncService(
        client=client,
        raw_payload_storage=FakeRawPayloadStorageService(),
    )

    result = service.sync_markets(include_closed=False, page_size=2, max_markets=1)
    output = capsys.readouterr().err

    assert result.fetched_count == 1
    assert "Starting market sync include_closed=False" in output
    assert "Using only open market filter" in output
    assert "Market sync stored created market" in output
    assert "Finished market sync fetched=1 created=1 updated=0" in output


def test_raw_payload_storage_writes_clickhouse_row() -> None:
    client = FakeClickHouseClient()
    storage = PolymarketMarketRawPayloadStorageService(client=client)
    payload = _market_payload(external_id="2869150", question="First question")

    storage.ensure_table()
    storage.insert_payload(_gamma_market(payload))

    assert "CREATE TABLE IF NOT EXISTS polymarket_market_raw_payloads" in client.commands[0]
    assert "ORDER BY (market_external_id, synced_at)" in client.commands[0]
    assert client.insert_table == "polymarket_market_raw_payloads"
    assert client.insert_column_names == (
        "synced_at",
        "market_external_id",
        "condition_id",
        "slug",
        "payload_json",
    )
    inserted_row = client.insert_rows[0]
    assert inserted_row[1] == "2869150"
    assert inserted_row[2] == "condition-2869150"
    assert inserted_row[3] == "market-2869150"
    assert inserted_row[4] == json.dumps(payload, separators=(",", ":"), sort_keys=True)


class FakeRawPayloadStorageService(PolymarketMarketRawPayloadStorageService):
    def __init__(self) -> None:
        self.table_ensured = False
        self.markets: list[PolymarketGammaMarket] = []

    def ensure_table(self) -> None:
        self.table_ensured = True

    def insert_payload(self, market: PolymarketGammaMarket) -> None:
        self.markets.append(market)


class FakeClickHouseClient(ClickHouseClient):
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.insert_table = ""
        self.insert_rows: list[tuple[object, ...]] = []
        self.insert_column_names: tuple[str, ...] = ()

    def command(self, query: str) -> None:
        self.commands.append(query)

    def insert(
        self,
        table: str,
        rows: Sequence[Sequence[object]],
        column_names: Sequence[str],
    ) -> None:
        self.insert_table = table
        self.insert_rows = [tuple(row) for row in rows]
        self.insert_column_names = tuple(column_names)
