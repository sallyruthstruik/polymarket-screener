from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import clickhouse_connect
from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.markets.clients.polymarket import (
    PolymarketClobPriceClient,
    PolymarketPriceRequest,
    PolymarketTokenPrice,
)
from apps.markets.models import PolymarketMarket


class ClickHouseSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str
    port: int
    database: str
    username: str
    password: str

    @classmethod
    def from_django_settings(cls) -> "ClickHouseSettings":
        raw_settings: dict[str, str] = settings.CLICKHOUSE
        return cls(
            host=raw_settings["HOST"],
            port=int(raw_settings["PORT"]),
            database=raw_settings["DATABASE"],
            username=raw_settings["USER"],
            password=raw_settings["PASSWORD"],
        )


class ClickHouseClient:
    def __init__(self, clickhouse_settings: ClickHouseSettings | None = None) -> None:
        resolved_settings = clickhouse_settings or ClickHouseSettings.from_django_settings()
        self._client: Any = clickhouse_connect.get_client(
            host=resolved_settings.host,
            port=resolved_settings.port,
            database=resolved_settings.database,
            username=resolved_settings.username,
            password=resolved_settings.password,
        )

    def command(self, query: str) -> None:
        self._client.command(query)

    def insert(
        self,
        table: str,
        rows: Sequence[Sequence[object]],
        column_names: Sequence[str],
    ) -> None:
        self._client.insert(table, rows, column_names=column_names)


class PolymarketPriceObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    market_external_id: str
    condition_id: str
    token_id: str
    side: str
    price: Decimal
    source: str


class PolymarketPriceSyncResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_count: int
    token_count: int
    price_count: int


class PolymarketPriceStorageService:
    table_name = "polymarket_prices"
    column_names = (
        "observed_at",
        "market_external_id",
        "condition_id",
        "token_id",
        "side",
        "price",
        "source",
    )

    def __init__(self, client: ClickHouseClient | None = None) -> None:
        self.client = client or ClickHouseClient()

    def ensure_table(self) -> None:
        self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name}
            (
                observed_at DateTime64(3, 'UTC'),
                market_external_id String,
                condition_id String,
                token_id String,
                side LowCardinality(String),
                price Decimal(18, 8),
                source LowCardinality(String)
            )
            ENGINE = MergeTree()
            PARTITION BY toYYYYMM(observed_at)
            ORDER BY (market_external_id, token_id, side, observed_at)
            """
        )

    def insert_observations(self, observations: Sequence[PolymarketPriceObservation]) -> int:
        if not observations:
            return 0
        rows: list[tuple[object, ...]] = [
            (
                observation.observed_at,
                observation.market_external_id,
                observation.condition_id,
                observation.token_id,
                observation.side,
                observation.price,
                observation.source,
            )
            for observation in observations
        ]
        self.client.insert(self.table_name, rows, self.column_names)
        return len(rows)


class PolymarketPriceSyncService:
    def __init__(
        self,
        *,
        clob_client: PolymarketClobPriceClient | None = None,
        storage: PolymarketPriceStorageService | None = None,
    ) -> None:
        self.clob_client = clob_client or PolymarketClobPriceClient()
        self.storage = storage or PolymarketPriceStorageService()

    def sync_prices(
        self,
        *,
        batch_size: int = 500,
        max_markets: int | None = None,
    ) -> PolymarketPriceSyncResult:
        self.storage.ensure_table()

        price_count = 0
        market_count = 0
        token_ids_seen: set[str] = set()

        for market_batch in self._iter_market_batches(
            batch_size=batch_size,
            max_markets=max_markets,
        ):
            market_count += len(market_batch)
            requests_by_token = self._build_price_requests(market_batch)
            token_ids_seen.update(token_id for token_id, _side in requests_by_token)
            prices = self.clob_client.fetch_prices(list(requests_by_token.values()))
            observations = self._build_observations(
                markets=market_batch,
                prices=prices,
                observed_at=timezone.now(),
            )
            price_count += self.storage.insert_observations(observations)

        return PolymarketPriceSyncResult(
            market_count=market_count,
            token_count=len(token_ids_seen),
            price_count=price_count,
        )

    def _iter_market_batches(
        self,
        *,
        batch_size: int,
        max_markets: int | None,
    ) -> Iterable[list[PolymarketMarket]]:
        markets = PolymarketMarket.objects.filter(sync_prices=True).order_by("external_id")
        if max_markets is not None:
            markets = markets[:max_markets]

        batch: list[PolymarketMarket] = []
        for market in markets:
            market_request_count = len(self._build_price_requests([market]))
            if market_request_count == 0:
                continue
            if batch and len(self._build_price_requests(batch)) + market_request_count > batch_size:
                yield batch
                batch = []
            batch.append(market)
            if len(self._build_price_requests(batch)) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def _build_price_requests(
        self,
        markets: Sequence[PolymarketMarket],
    ) -> dict[tuple[str, str], PolymarketPriceRequest]:
        requests: dict[tuple[str, str], PolymarketPriceRequest] = {}
        for market in markets:
            for token_id in self._get_token_ids(market):
                requests[(token_id, "BUY")] = PolymarketPriceRequest(token_id=token_id, side="BUY")
                requests[(token_id, "SELL")] = PolymarketPriceRequest(
                    token_id=token_id,
                    side="SELL",
                )
        return requests

    def _build_observations(
        self,
        *,
        markets: Sequence[PolymarketMarket],
        prices: Sequence[PolymarketTokenPrice],
        observed_at: datetime,
    ) -> list[PolymarketPriceObservation]:
        normalized_observed_at = self._normalize_observed_at(observed_at)
        markets_by_token = {
            token_id: market for market in markets for token_id in self._get_token_ids(market)
        }
        observations: list[PolymarketPriceObservation] = []
        for price in prices:
            market = markets_by_token.get(price.token_id)
            if market is None:
                continue
            observations.append(
                PolymarketPriceObservation(
                    observed_at=normalized_observed_at,
                    market_external_id=market.external_id,
                    condition_id=market.condition_id,
                    token_id=price.token_id,
                    side=price.side,
                    price=price.price,
                    source="clob_prices",
                )
            )
        return observations

    def _get_token_ids(self, market: PolymarketMarket) -> list[str]:
        return [str(value) for value in market.clob_token_ids if isinstance(value, str) and value]

    def _normalize_observed_at(self, observed_at: datetime) -> datetime:
        if timezone.is_naive(observed_at):
            return timezone.make_aware(observed_at, UTC)
        return observed_at.astimezone(UTC)
