import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from apps.markets.clients.polymarket import PolymarketGammaClient, PolymarketGammaMarket
from apps.markets.services.clickhouse import ClickHouseClient
from apps.markets.services.polymarket import (
    PolymarketMarketAdminInput,
    PolymarketMarketRawPayloadStorageService,
    PolymarketMarketStorageService,
    PolymarketMarketSyncResult,
    PolymarketMarketSyncService,
    PolymarketMarketVersionGenerator,
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
        "category": "sports",
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


class FakeClickHouseClient(ClickHouseClient):
    def __init__(self, query_results: Sequence[Sequence[Sequence[object]]] | None = None) -> None:
        self.commands: list[str] = []
        self.insert_table = ""
        self.insert_rows: list[tuple[object, ...]] = []
        self.insert_column_names: tuple[str, ...] = ()
        self.queries: list[str] = []
        self.query_results = list(query_results or [])

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

    def query(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> Sequence[Sequence[object]]:
        self.queries.append(query)
        if not self.query_results:
            return []
        return self.query_results.pop(0)


def test_market_storage_creates_table_and_inserts_rows_preserving_sync_prices() -> None:
    existing_row = (
        "2869150",
        "condition-2869150",
        "market-2869150",
        "Old question",
        "Old description",
        "sports",
        True,
        False,
        False,
        True,
        True,
        datetime(2026, 7, 10, 10, 27, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 30, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 28, tzinfo=UTC),
        datetime(2026, 7, 11, 10, 25, tzinfo=UTC),
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
        Decimal("4.00"),
        Decimal("5.00"),
        ["token-a", "token-b"],
        True,
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        1,
        "sync",
    )
    client = FakeClickHouseClient(query_results=[[existing_row]])
    storage = PolymarketMarketStorageService(client=client)
    payload = _market_payload(external_id="2869150", question="Updated question")

    storage.ensure_table()
    result = storage.insert_markets([_gamma_market(payload)])

    assert "CREATE TABLE IF NOT EXISTS polymarket_markets" in client.commands[0]
    assert result.fetched_count == 1
    assert result.created_count == 0
    assert result.updated_count == 1
    inserted_row = client.insert_rows[0]
    assert inserted_row[0] == "2869150"
    assert inserted_row[3] == "Updated question"
    assert inserted_row[21] is True
    assert inserted_row[22] == datetime(2026, 7, 10, 10, 31, tzinfo=UTC)


def test_market_storage_saves_admin_edit_as_replacement_row() -> None:
    existing_row = (
        "2869150",
        "condition-2869150",
        "market-2869150",
        "Question",
        "Description",
        "sports",
        True,
        False,
        False,
        True,
        True,
        datetime(2026, 7, 10, 10, 27, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 30, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 28, tzinfo=UTC),
        datetime(2026, 7, 11, 10, 25, tzinfo=UTC),
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
        Decimal("4.00"),
        Decimal("5.00"),
        ["token-a", "token-b"],
        False,
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        datetime(2026, 7, 10, 10, 31, tzinfo=UTC),
        1,
        "sync",
    )
    client = FakeClickHouseClient(query_results=[[existing_row]])
    storage = PolymarketMarketStorageService(client=client)

    updated = storage.save_admin_edit(
        external_id="2869150",
        market_input=PolymarketMarketAdminInput(
            condition_id="condition-2869150",
            slug="market-2869150",
            question="Edited question",
            description="Edited description",
            category="politics",
            active=True,
            closed=False,
            archived=False,
            restricted=True,
            accepting_orders=True,
            market_created_at=datetime(2026, 7, 10, 10, 27, tzinfo=UTC),
            market_updated_at=datetime(2026, 7, 10, 10, 30, tzinfo=UTC),
            start_date=datetime(2026, 7, 10, 10, 28, tzinfo=UTC),
            end_date=datetime(2026, 7, 11, 10, 25, tzinfo=UTC),
            liquidity=Decimal("1.50"),
            volume=Decimal("2.50"),
            liquidity_clob=Decimal("3.50"),
            volume_clob=Decimal("4.50"),
            volume_24hr=Decimal("5.50"),
            clob_token_ids=["token-a", "token-b"],
            sync_prices=True,
        ),
    )

    assert updated.write_source == "admin"
    assert updated.sync_prices is True
    assert updated.question == "Edited question"
    assert client.insert_rows[0][26] == "admin"


def test_market_version_generator_is_monotonic() -> None:
    generator = PolymarketMarketVersionGenerator()

    first = generator.next_version()
    second = generator.next_version()

    assert second > first


def test_sync_service_batches_market_and_raw_payload_writes() -> None:
    client = FakeGammaClient()
    structured_storage = FakeStructuredStorage()
    raw_payload_storage = FakeRawPayloadStorageService()
    service = PolymarketMarketSyncService(
        client=client,
        storage=structured_storage,
        raw_payload_storage=raw_payload_storage,
    )
    service.market_batch_size = 1
    service.raw_payload_batch_size = 1
    service.raw_payload_batch_bytes = 1

    result = service.sync_markets(include_closed=True, page_size=2, max_markets=3)

    assert client.closed_filters == [False, True]
    assert structured_storage.table_ensured is True
    assert raw_payload_storage.table_ensured is True
    assert structured_storage.insert_batch_sizes == [1, 1, 1]
    assert raw_payload_storage.insert_batch_sizes == [1, 1, 1]
    assert result.fetched_count == 3


def test_raw_payload_storage_writes_clickhouse_rows() -> None:
    client = FakeClickHouseClient()
    storage = PolymarketMarketRawPayloadStorageService(client=client)
    payload = _market_payload(external_id="2869150", question="First question")

    storage.ensure_table()
    inserted_count = storage.insert_payloads([_gamma_market(payload)])

    assert inserted_count == 1
    assert "CREATE TABLE IF NOT EXISTS polymarket_market_raw_payloads" in client.commands[0]
    assert client.insert_table == "polymarket_market_raw_payloads"
    inserted_row = client.insert_rows[0]
    assert inserted_row[1] == "2869150"
    assert inserted_row[4] == json.dumps(payload, separators=(",", ":"), sort_keys=True)


class FakeStructuredStorage(PolymarketMarketStorageService):
    def __init__(self) -> None:
        self.table_ensured = False
        self.insert_batch_sizes: list[int] = []

    def ensure_table(self) -> None:
        self.table_ensured = True

    def insert_markets(
        self,
        markets: Sequence[PolymarketGammaMarket],
    ) -> PolymarketMarketSyncResult:
        self.insert_batch_sizes.append(len(markets))
        return PolymarketMarketSyncResult(
            fetched_count=len(markets),
            created_count=len(markets),
            updated_count=0,
        )


class FakeRawPayloadStorageService(PolymarketMarketRawPayloadStorageService):
    def __init__(self) -> None:
        self.table_ensured = False
        self.insert_batch_sizes: list[int] = []

    def ensure_table(self) -> None:
        self.table_ensured = True

    def insert_payloads(self, markets: Sequence[PolymarketGammaMarket]) -> int:
        self.insert_batch_sizes.append(len(markets))
        return len(markets)
