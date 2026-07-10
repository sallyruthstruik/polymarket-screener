from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.markets.clients.polymarket import PolymarketClobPriceClient, PolymarketPriceHistoryPoint
from apps.markets.models import PolymarketMarket
from apps.markets.services.clickhouse import ClickHouseClient


class PolymarketPriceObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    market_external_id: str
    condition_id: str
    token_id: str
    side: str
    price: Decimal
    source: str


class PolymarketPriceInspectionRow(BaseModel):
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


class PolymarketPriceResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    fidelity_minutes: int
    lookback: timedelta | None


class PolymarketPriceStorageService:
    table_name = "polymarket_prices"
    history_side = "MID"
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

    def get_latest_history_timestamp(self, *, token_id: str, source: str) -> datetime | None:
        rows = self.client.query(
            f"""
            SELECT max(observed_at)
            FROM {self.table_name}
            WHERE token_id = %(token_id)s
              AND side = %(side)s
              AND source = %(source)s
            """,
            parameters={
                "token_id": token_id,
                "side": self.history_side,
                "source": source,
            },
        )
        if not rows:
            return None
        raw_timestamp = rows[0][0]
        if not isinstance(raw_timestamp, datetime):
            return None
        if timezone.is_naive(raw_timestamp):
            return timezone.make_aware(raw_timestamp, UTC)
        return raw_timestamp.astimezone(UTC)

    def list_observations(
        self,
        *,
        market_external_id: str | None = None,
        token_id: str | None = None,
        limit: int = 100,
    ) -> list[PolymarketPriceInspectionRow]:
        filters = ["1 = 1"]
        parameters: dict[str, object] = {"limit": limit}
        if market_external_id is not None:
            filters.append("market_external_id = %(market_external_id)s")
            parameters["market_external_id"] = market_external_id
        if token_id is not None:
            filters.append("token_id = %(token_id)s")
            parameters["token_id"] = token_id

        rows = self.client.query(
            f"""
            SELECT observed_at, market_external_id, condition_id, token_id, side, price, source
            FROM {self.table_name}
            WHERE {' AND '.join(filters)}
            ORDER BY observed_at DESC
            LIMIT %(limit)s
            """,
            parameters=parameters,
        )

        observations: list[PolymarketPriceInspectionRow] = []
        for row in rows:
            observed_at, market_id, condition_id, row_token_id, side, price, source = row
            if not isinstance(observed_at, datetime):
                continue
            parsed_price = self._parse_price_decimal(price)
            if parsed_price is None:
                continue
            observations.append(
                PolymarketPriceInspectionRow(
                    observed_at=self._normalize_observed_at(observed_at),
                    market_external_id=str(market_id),
                    condition_id=str(condition_id),
                    token_id=str(row_token_id),
                    side=str(side),
                    price=parsed_price,
                    source=str(source),
                )
            )
        return observations

    def _parse_price_decimal(self, value: object) -> Decimal | None:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int | float | str):
            return Decimal(str(value))
        return None

    def _normalize_observed_at(self, observed_at: datetime) -> datetime:
        if timezone.is_naive(observed_at):
            return timezone.make_aware(observed_at, UTC)
        return observed_at.astimezone(UTC)

class PolymarketPriceSyncService:
    default_resolutions = (
        PolymarketPriceResolution(
            fidelity_minutes=60,
            lookback=timedelta(days=30),
        ),
        PolymarketPriceResolution(
            fidelity_minutes=60 * 24,
            lookback=None,
        ),
    )

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
        fidelity_minutes: int | None = None,
        chunk_size_minutes: int = 60 * 24,
    ) -> PolymarketPriceSyncResult:
        self.storage.ensure_table()

        price_count = 0
        market_count = 0
        token_ids_seen: set[str] = set()
        resolutions = self._get_resolutions(fidelity_minutes)

        for market_batch in self._iter_market_batches(
            batch_size=batch_size,
            max_markets=max_markets,
        ):
            market_count += len(market_batch)
            for market in market_batch:
                for token_id in self._get_token_ids(market):
                    token_ids_seen.add(token_id)
                    for resolution in resolutions:
                        price_count += self._sync_token_history(
                            market=market,
                            token_id=token_id,
                            resolution=resolution,
                            chunk_size_minutes=chunk_size_minutes,
                            single_resolution_mode=fidelity_minutes is not None,
                        )

        return PolymarketPriceSyncResult(
            market_count=market_count,
            token_count=len(token_ids_seen),
            price_count=price_count,
        )

    def _sync_token_history(
        self,
        *,
        market: PolymarketMarket,
        token_id: str,
        resolution: PolymarketPriceResolution,
        chunk_size_minutes: int,
        single_resolution_mode: bool,
    ) -> int:
        start_timestamp = self._get_history_start_timestamp(
            market=market,
            token_id=token_id,
            resolution=resolution,
        )
        end_timestamp = self._get_history_end_timestamp(
            resolution=resolution,
            single_resolution_mode=single_resolution_mode,
        )
        if start_timestamp > end_timestamp:
            return 0

        inserted_count = 0
        current_start = start_timestamp
        chunk_delta = timedelta(minutes=chunk_size_minutes)

        while current_start <= end_timestamp:
            current_end = min(current_start + chunk_delta, end_timestamp)
            history = self.clob_client.fetch_price_history(
                token_id=token_id,
                start_timestamp=current_start,
                end_timestamp=current_end,
                fidelity_minutes=resolution.fidelity_minutes,
            )
            observations = self._build_history_observations(
                market=market,
                token_id=token_id,
                history=history,
                resolution=resolution,
            )
            inserted_count += self.storage.insert_observations(observations)
            current_start = current_end + timedelta(minutes=resolution.fidelity_minutes)

        return inserted_count

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
        request_count = 0
        for market in markets:
            token_count = len(self._get_token_ids(market))
            if token_count == 0:
                continue
            if batch and request_count + token_count > batch_size:
                yield batch
                batch = []
                request_count = 0
            batch.append(market)
            request_count += token_count

        if batch:
            yield batch

    def _build_history_observations(
        self,
        *,
        market: PolymarketMarket,
        token_id: str,
        history: Sequence[PolymarketPriceHistoryPoint],
        resolution: PolymarketPriceResolution,
    ) -> list[PolymarketPriceObservation]:
        return [
            PolymarketPriceObservation(
                observed_at=self._normalize_observed_at(point.timestamp),
                market_external_id=market.external_id,
                condition_id=market.condition_id,
                token_id=token_id,
                side=self.storage.history_side,
                price=point.price,
                source=self._get_history_source(resolution.fidelity_minutes),
            )
            for point in history
        ]

    def _get_history_start_timestamp(
        self,
        *,
        market: PolymarketMarket,
        token_id: str,
        resolution: PolymarketPriceResolution,
    ) -> datetime:
        latest_timestamp = self.storage.get_latest_history_timestamp(
            token_id=token_id,
            source=self._get_history_source(resolution.fidelity_minutes),
        )
        if latest_timestamp is not None:
            return latest_timestamp + timedelta(minutes=resolution.fidelity_minutes)

        market_timestamp = market.market_created_at or market.start_date or timezone.now()
        normalized_market_timestamp = self._normalize_observed_at(market_timestamp)
        resolution_start = self._get_resolution_start_timestamp(resolution=resolution)
        if resolution_start is None:
            return normalized_market_timestamp
        return max(
            normalized_market_timestamp,
            resolution_start,
        )

    def _get_history_end_timestamp(
        self,
        *,
        resolution: PolymarketPriceResolution,
        single_resolution_mode: bool,
    ) -> datetime:
        now = timezone.now().astimezone(UTC)
        if single_resolution_mode:
            return now
        if resolution.lookback is None and self.default_resolutions:
            previous_resolution = self.default_resolutions[0]
            previous_resolution_start = self._get_resolution_start_timestamp(
                resolution=previous_resolution
            )
            if previous_resolution_start is None:
                return now
            return previous_resolution_start - timedelta(minutes=1)
        return now

    def _get_resolution_start_timestamp(
        self,
        *,
        resolution: PolymarketPriceResolution,
    ) -> datetime | None:
        if resolution.lookback is None:
            return None
        return timezone.now().astimezone(UTC) - resolution.lookback

    def _get_resolutions(
        self,
        fidelity_minutes: int | None,
    ) -> tuple[PolymarketPriceResolution, ...]:
        if fidelity_minutes is not None:
            return (PolymarketPriceResolution(fidelity_minutes=fidelity_minutes, lookback=None),)
        return self.default_resolutions

    def _get_history_source(self, fidelity_minutes: int) -> str:
        return f"clob_prices_history_{fidelity_minutes}m"

    def _get_token_ids(self, market: PolymarketMarket) -> list[str]:
        return [str(value) for value in market.clob_token_ids if isinstance(value, str) and value]

    def _normalize_observed_at(self, observed_at: datetime) -> datetime:
        return self.storage._normalize_observed_at(observed_at)
