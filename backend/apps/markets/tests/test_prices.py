from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.markets.clients.polymarket import (
    PolymarketClobPriceClient,
    PolymarketPriceRequest,
    PolymarketTokenPrice,
)
from apps.markets.models import PolymarketMarket
from apps.markets.services.prices import (
    ClickHouseClient,
    PolymarketPriceObservation,
    PolymarketPriceStorageService,
    PolymarketPriceSyncService,
)


@pytest.mark.django_db
def test_price_sync_service_uses_only_markets_enabled_for_price_sync() -> None:
    enabled_market = _create_market(external_id="1", sync_prices=True)
    _create_market(external_id="2", sync_prices=False)
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService()

    result = PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
    ).sync_prices(batch_size=10)

    assert storage.ensure_table_called is True
    assert result.market_count == 1
    assert result.token_count == 2
    assert result.price_count == 4
    assert clob_client.requests == [
        PolymarketPriceRequest(token_id="token-1-yes", side="BUY"),
        PolymarketPriceRequest(token_id="token-1-yes", side="SELL"),
        PolymarketPriceRequest(token_id="token-1-no", side="BUY"),
        PolymarketPriceRequest(token_id="token-1-no", side="SELL"),
    ]
    assert [observation.market_external_id for observation in storage.observations] == [
        enabled_market.external_id,
        enabled_market.external_id,
        enabled_market.external_id,
        enabled_market.external_id,
    ]


def test_clob_price_client_parses_batch_price_response() -> None:
    client = PolymarketClobPriceClient()

    prices = client._parse_prices_response(
        {
            "token-a": {"BUY": "0.52", "SELL": "0.48"},
            "token-b": {"BUY": "", "SELL": "0.2"},
        },
        [
            PolymarketPriceRequest(token_id="token-a", side="BUY"),
            PolymarketPriceRequest(token_id="token-a", side="SELL"),
            PolymarketPriceRequest(token_id="token-b", side="BUY"),
            PolymarketPriceRequest(token_id="token-b", side="SELL"),
        ],
    )

    assert prices == [
        PolymarketTokenPrice(token_id="token-a", side="BUY", price=Decimal("0.52")),
        PolymarketTokenPrice(token_id="token-a", side="SELL", price=Decimal("0.48")),
        PolymarketTokenPrice(token_id="token-b", side="SELL", price=Decimal("0.2")),
    ]


def test_price_storage_creates_table_and_inserts_rows() -> None:
    clickhouse_client = FakeClickHouseClient()
    storage = PolymarketPriceStorageService(client=clickhouse_client)
    observed_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    storage.ensure_table()
    inserted_count = storage.insert_observations(
        [
            PolymarketPriceObservation(
                observed_at=observed_at,
                market_external_id="1",
                condition_id="condition-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.52"),
                source="clob_prices",
            )
        ]
    )

    assert inserted_count == 1
    assert "CREATE TABLE IF NOT EXISTS polymarket_prices" in clickhouse_client.commands[0]
    assert clickhouse_client.insert_table == "polymarket_prices"
    assert clickhouse_client.insert_rows == [
        (
            observed_at,
            "1",
            "condition-1",
            "token-1",
            "BUY",
            Decimal("0.52"),
            "clob_prices",
        )
    ]


class FakeClobPriceClient(PolymarketClobPriceClient):
    def __init__(self) -> None:
        self.requests: list[PolymarketPriceRequest] = []

    def fetch_prices(self, requests: list[PolymarketPriceRequest]) -> list[PolymarketTokenPrice]:
        self.requests.extend(requests)
        return [
            PolymarketTokenPrice(
                token_id=request.token_id,
                side=request.side,
                price=Decimal("0.5"),
            )
            for request in requests
        ]


class FakePriceStorageService(PolymarketPriceStorageService):
    def __init__(self) -> None:
        self.ensure_table_called = False
        self.observations: list[PolymarketPriceObservation] = []

    def ensure_table(self) -> None:
        self.ensure_table_called = True

    def insert_observations(self, observations: Sequence[PolymarketPriceObservation]) -> int:
        self.observations.extend(observations)
        return len(observations)


class FakeClickHouseClient(ClickHouseClient):
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.insert_table = ""
        self.insert_rows: Sequence[Sequence[object]] = []
        self.insert_column_names: Sequence[str] = []

    def command(self, query: str) -> None:
        self.commands.append(query)

    def insert(
        self,
        table: str,
        rows: Sequence[Sequence[object]],
        column_names: Sequence[str],
    ) -> None:
        self.insert_table = table
        self.insert_rows = rows
        self.insert_column_names = column_names


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
