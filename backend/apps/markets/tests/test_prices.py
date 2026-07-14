from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.markets.clients.polymarket import PolymarketClobPriceClient, PolymarketPriceHistoryPoint
from apps.markets.models import PolymarketMarket
from apps.markets.services.clickhouse import ClickHouseClient
from apps.markets.services.prices import (
    PolymarketPriceInspectionRow,
    PolymarketPriceObservation,
    PolymarketPriceStorageService,
    PolymarketPriceSyncService,
)


@pytest.mark.django_db
def test_price_sync_service_uses_only_markets_enabled_for_price_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_now = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: frozen_now)
    enabled_market = _create_market(
        external_id="1",
        sync_prices=True,
        market_created_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
    )
    _create_market(external_id="2", sync_prices=False, market_created_at=None)
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService()

    result = PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
    ).sync_prices(
        batch_size=10,
        chunk_size_minutes=60 * 24,
    )

    assert storage.ensure_table_called is True
    assert result.market_count == 1
    assert result.token_count == 2
    assert result.price_count == 4
    assert len(clob_client.history_requests) == 2
    assert [observation.market_external_id for observation in storage.observations] == [
        enabled_market.external_id,
        enabled_market.external_id,
        enabled_market.external_id,
        enabled_market.external_id,
    ]
    assert all(observation.side == "MID" for observation in storage.observations)
    assert {observation.source for observation in storage.observations} == {
        "clob_prices_history_60m"
    }


@pytest.mark.django_db
def test_price_sync_service_resumes_from_latest_history_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_now = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: frozen_now)
    market = _create_market(
        external_id="1",
        sync_prices=True,
        market_created_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
    )
    latest_timestamp = datetime(2026, 7, 10, 11, 5, tzinfo=UTC)
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService(latest_history_timestamp=latest_timestamp)

    PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
    ).sync_prices(
        batch_size=10,
        chunk_size_minutes=60 * 24,
    )

    assert len(clob_client.history_requests) == 2
    expected_start_timestamp = latest_timestamp + timedelta(minutes=60)
    hourly_requests = [
        request for request in clob_client.history_requests if request["fidelity_minutes"] == 60
    ]
    assert len(hourly_requests) == 2
    assert all(
        request["start_timestamp"] == expected_start_timestamp
        for request in hourly_requests
    )
    assert all(
        observation.market_external_id == market.external_id
        for observation in storage.observations
    )


@pytest.mark.django_db
def test_price_sync_service_uses_daily_backfill_for_older_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_now = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: frozen_now)
    old_market = _create_market(
        external_id="1",
        sync_prices=True,
        market_created_at=frozen_now - timedelta(days=45),
    )
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService()

    result = PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
    ).sync_prices(
        batch_size=10,
        chunk_size_minutes=60 * 24,
    )

    assert result.price_count > 4
    assert len(clob_client.history_requests) > 2
    assert {request["fidelity_minutes"] for request in clob_client.history_requests} == {60, 1440}
    assert {observation.source for observation in storage.observations} == {
        "clob_prices_history_60m",
        "clob_prices_history_1440m",
    }
    assert all(
        observation.market_external_id == old_market.external_id
        for observation in storage.observations
    )


@pytest.mark.django_db
def test_price_sync_service_can_force_single_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_now = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: frozen_now)
    _create_market(
        external_id="1",
        sync_prices=True,
        market_created_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
    )
    clob_client = FakeClobPriceClient()
    storage = FakePriceStorageService()

    PolymarketPriceSyncService(
        clob_client=clob_client,
        storage=storage,
    ).sync_prices(
        batch_size=10,
        fidelity_minutes=60,
        chunk_size_minutes=60 * 24,
    )

    assert len(clob_client.history_requests) == 2
    assert all(request["fidelity_minutes"] == 60 for request in clob_client.history_requests)


def test_clob_price_client_parses_price_history_response() -> None:
    client = PolymarketClobPriceClient()
    end_timestamp = datetime(2026, 7, 10, 12, 2, tzinfo=UTC)
    first_timestamp = int(end_timestamp.timestamp()) - 120
    second_timestamp = int(end_timestamp.timestamp()) - 60
    future_timestamp = int(end_timestamp.timestamp()) + 600

    history = client._parse_price_history_response(
        {
            "history": [
                {"t": first_timestamp, "p": 0.495},
                {"t": second_timestamp, "p": "0.500"},
                {"t": future_timestamp, "p": "0.700"},
            ]
        },
        end_timestamp=end_timestamp,
    )

    assert history == [
        PolymarketPriceHistoryPoint(
            timestamp=datetime.fromtimestamp(first_timestamp, tz=UTC),
            price=Decimal("0.495"),
        ),
        PolymarketPriceHistoryPoint(
            timestamp=datetime.fromtimestamp(second_timestamp, tz=UTC),
            price=Decimal("0.500"),
        ),
    ]


def test_price_storage_creates_table_inserts_rows_and_reads_latest_history_timestamp() -> None:
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
    assert clickhouse_client.insert_rows == [
        (
            observed_at,
            "1",
            "condition-1",
            "token-1",
            "MID",
            Decimal("0.52"),
            "clob_prices_history",
        )
    ]
    assert latest_timestamp == observed_at


def test_price_storage_lists_rows() -> None:
    clickhouse_client = FakeClickHouseClient()
    clickhouse_client.query_rows = [
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
    assert clickhouse_client.query_parameters == {
        "market_external_id": "1",
        "token_id": "token-1",
        "limit": 25,
    }


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
            PolymarketPriceHistoryPoint(
                timestamp=start_timestamp,
                price=Decimal("0.5"),
            ),
            PolymarketPriceHistoryPoint(
                timestamp=start_timestamp + timedelta(minutes=1),
                price=Decimal("0.51"),
            ),
        ]


class FakePriceStorageService(PolymarketPriceStorageService):
    def __init__(self, latest_history_timestamp: datetime | None = None) -> None:
        self.ensure_table_called = False
        self.observations: list[PolymarketPriceObservation] = []
        self.latest_history_timestamp = latest_history_timestamp

    def ensure_table(self) -> None:
        self.ensure_table_called = True

    def insert_observations(self, observations: Sequence[PolymarketPriceObservation]) -> int:
        self.observations.extend(observations)
        return len(observations)

    def get_latest_history_timestamp(self, *, token_id: str, source: str) -> datetime | None:
        if source == "clob_prices_history_60m":
            return self.latest_history_timestamp
        return None


class FakeClickHouseClient(ClickHouseClient):
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.insert_table = ""
        self.insert_rows: Sequence[Sequence[object]] = []
        self.insert_column_names: Sequence[str] = []
        self.query_rows: Sequence[Sequence[object]] = []
        self.query_sql = ""
        self.query_parameters: dict[str, object] | None = None

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

    def query(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> Sequence[Sequence[object]]:
        self.query_sql = query
        self.query_parameters = parameters
        if parameters is not None and "source" in parameters:
            return [[datetime(2026, 7, 10, 12, 0, tzinfo=UTC)]]
        return self.query_rows


def _create_market(
    *,
    external_id: str,
    sync_prices: bool,
    market_created_at: datetime | None,
) -> PolymarketMarket:
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
        market_created_at=market_created_at,
        clob_token_ids=[f"token-{external_id}-yes", f"token-{external_id}-no"],
        sync_prices=sync_prices,
    )
