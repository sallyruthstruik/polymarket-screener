from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.markets.clients.polymarket import PolymarketClobPriceClient, PolymarketPriceHistoryPoint
from apps.markets.services.clickhouse import ClickHouseClient
from apps.markets.services.polymarket import PolymarketMarketData, PolymarketMarketStorageService
from apps.markets.services.prices import (
    PolymarketPriceInspectionRow,
    PolymarketPriceObservation,
    PolymarketPriceStorageService,
    PolymarketPriceSyncService,
)


class FakeClobPriceClient(PolymarketClobPriceClient):
    def __init__(self) -> None:
        self.history_requests: list[dict[str, object]] = []

    def fetch_price_history(
        self,
        *,
        token_id: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        fidelity_minutes: int,
    ) -> list[PolymarketPriceHistoryPoint]:
        self.history_requests.append(
            {
                "token_id": token_id,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "fidelity_minutes": fidelity_minutes,
            }
        )
        return [
            PolymarketPriceHistoryPoint(timestamp=start_timestamp, price=Decimal("0.5")),
            PolymarketPriceHistoryPoint(
                timestamp=start_timestamp + timedelta(minutes=1),
                price=Decimal("0.51"),
            ),
        ]


class FakePriceStorageService(PolymarketPriceStorageService):
    def __init__(
        self,
        *,
        latest_history_timestamp: datetime | None = None,
        chart_rows: Sequence[Sequence[object]] | None = None,
    ) -> None:
        self.ensure_table_called = False
        self.observations: list[PolymarketPriceObservation] = []
        self.latest_history_timestamp = latest_history_timestamp
        self.chart_rows = list(chart_rows or [])

    def ensure_table(self) -> None:
        self.ensure_table_called = True

    def insert_observations(self, observations: Sequence[PolymarketPriceObservation]) -> int:
        self.observations.extend(observations)
        return len(observations)

    def get_latest_history_timestamp(self, *, token_id: str, source: str) -> datetime | None:
        return self.latest_history_timestamp


class FakeMarketStorageService(PolymarketMarketStorageService):
    def __init__(self, markets: Sequence[PolymarketMarketData]) -> None:
        self.markets = list(markets)
        self.calls: list[dict[str, int]] = []

    def list_price_sync_markets(self, *, limit: int, offset: int) -> list[PolymarketMarketData]:
        self.calls.append({"limit": limit, "offset": offset})
        return self.markets[offset : offset + limit]


class FakeClickHouseClient(ClickHouseClient):
    def __init__(self, query_rows: Sequence[Sequence[object]] | None = None) -> None:
        self.commands: list[str] = []
        self.insert_table = ""
        self.insert_rows: list[tuple[object, ...]] = []
        self.query_sql = ""
        self.query_rows = list(query_rows or [])

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

    def query(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> Sequence[Sequence[object]]:
        self.query_sql = query
        return self.query_rows


def _market(
    *,
    external_id: str,
    sync_prices: bool,
    market_created_at: datetime | None,
) -> PolymarketMarketData:
    now = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)
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
        market_created_at=market_created_at,
        market_updated_at=market_created_at,
        start_date=market_created_at,
        end_date=market_created_at,
        liquidity=Decimal("1.0"),
        volume=Decimal("2.0"),
        liquidity_clob=Decimal("3.0"),
        volume_clob=Decimal("4.0"),
        volume_24hr=Decimal("5.0"),
        clob_token_ids=(
            [f"token-{external_id}-yes", f"token-{external_id}-no"] if sync_prices else []
        ),
        sync_prices=sync_prices,
        first_synced_at=now,
        last_synced_at=now,
        written_at=now,
        row_version=1,
        write_source="sync",
    )


def test_price_sync_service_uses_clickhouse_enabled_markets_only() -> None:
    market_storage = FakeMarketStorageService(
        [
            _market(
                external_id="1",
                sync_prices=True,
                market_created_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
            ),
            _market(external_id="2", sync_prices=False, market_created_at=None),
        ]
    )
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService()

    result = PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
        market_storage=market_storage,
    ).sync_prices(batch_size=10, chunk_size_minutes=60 * 24)

    assert storage.ensure_table_called is True
    assert result.market_count == 1
    assert result.token_count == 2
    assert result.price_count > 4
    assert len(clob_client.history_requests) >= 2


def test_price_sync_service_resumes_from_latest_history_timestamp() -> None:
    latest_timestamp = datetime(2026, 7, 10, 11, 5, tzinfo=UTC)
    market_storage = FakeMarketStorageService(
        [
            _market(
                external_id="1",
                sync_prices=True,
                market_created_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
            )
        ]
    )
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService(latest_history_timestamp=latest_timestamp)

    PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
        market_storage=market_storage,
    ).sync_prices(batch_size=10, chunk_size_minutes=60 * 24)

    expected_start_timestamp = latest_timestamp + timedelta(minutes=60)
    hourly_requests = [
        request
        for request in clob_client.history_requests
        if request["fidelity_minutes"] == 60
    ]
    assert hourly_requests
    hourly_starts = [request["start_timestamp"] for request in hourly_requests]
    assert hourly_starts[0] == expected_start_timestamp
    assert all(
        isinstance(start_timestamp, datetime) and start_timestamp >= expected_start_timestamp
        for start_timestamp in hourly_starts
    )


def test_price_storage_creates_table_inserts_rows_and_reads_latest_history_timestamp() -> None:
    observed_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    clickhouse_client = FakeClickHouseClient(query_rows=[(observed_at,)])
    storage = PolymarketPriceStorageService(client=clickhouse_client)

    storage.ensure_table()
    inserted_count = storage.insert_observations(
        [
            PolymarketPriceObservation(
                observed_at=observed_at,
                market_external_id="1",
                condition_id="condition-1",
                token_id="token-1",
                side="MID",
                price=Decimal("0.52"),
                source="clob_prices_history",
            )
        ]
    )
    latest_timestamp = storage.get_latest_history_timestamp(
        token_id="token-1",
        source="clob_prices_history_60m",
    )

    assert inserted_count == 1
    assert "CREATE TABLE IF NOT EXISTS polymarket_prices" in clickhouse_client.commands[0]
    assert clickhouse_client.insert_table == "polymarket_prices"
    assert latest_timestamp == observed_at


def test_price_storage_builds_chart_with_source_fallback() -> None:
    chart_rows = [
        (
            datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            "1",
            "condition-1",
            "token-1",
            "MID",
            Decimal("0.52"),
            "clob_prices_history_1440m",
        ),
        (
            datetime(2026, 7, 10, 13, 0, tzinfo=UTC),
            "1",
            "condition-1",
            "token-2",
            "MID",
            Decimal("0.48"),
            "clob_prices_history_1440m",
        ),
    ]
    clickhouse_client = FakeClickHouseClient(query_rows=chart_rows)
    storage = PolymarketPriceStorageService(client=clickhouse_client)

    chart = storage.build_price_chart(
        market_external_id="1",
        token_ids=["token-1", "token-2"],
        range_key="all",
    )

    assert chart.resolved_source == "clob_prices_history_1440m"
    assert len(chart.series) == 2
    assert "source = 'clob_prices_history_1440m'" in clickhouse_client.query_sql


def test_price_storage_lists_rows() -> None:
    clickhouse_client = FakeClickHouseClient(
        query_rows=[
            (
                datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
                "1",
                "condition-1",
                "token-1",
                "MID",
                Decimal("0.52"),
                "clob_prices_history",
            )
        ]
    )
    storage = PolymarketPriceStorageService(client=clickhouse_client)

    rows = storage.list_observations(market_external_id="1", token_id="token-1", limit=25)

    assert rows == [
        PolymarketPriceInspectionRow(
            observed_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            market_external_id="1",
            condition_id="condition-1",
            token_id="token-1",
            side="MID",
            price=Decimal("0.52"),
            source="clob_prices_history",
        )
    ]
    assert "market_external_id = %(market_external_id)s" in clickhouse_client.query_sql
    assert "token_id = %(token_id)s" in clickhouse_client.query_sql
