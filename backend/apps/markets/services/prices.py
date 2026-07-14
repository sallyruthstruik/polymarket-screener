from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.core.logging import get_logger, should_log_progress
from apps.markets.clients.polymarket import PolymarketClobPriceClient, PolymarketPriceHistoryPoint
from apps.markets.models import PolymarketMarket
from apps.markets.services.clickhouse import ClickHouseClient

logger = get_logger("apps.markets.services.prices")


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
        logger.info("Ensuring prices ClickHouse table exists table=%s", self.table_name)
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
            logger.info("Skipping price insert because observation list is empty")
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
        logger.info("Inserted price observations count=%s", len(rows))
        return len(rows)

    def get_latest_history_timestamp(self, *, token_id: str, source: str) -> datetime | None:
        logger.info("Loading latest history timestamp token_id=%s source=%s", token_id, source)
        rows = self.client.query(
            f"""
            SELECT observed_at
            FROM {self.table_name}
            WHERE token_id = %(token_id)s
              AND side = %(side)s
              AND source = %(source)s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            parameters={
                "token_id": token_id,
                "side": self.history_side,
                "source": source,
            },
        )
        if not rows:
            logger.info(
                "Latest history timestamp missing because query returned no rows "
                "token_id=%s source=%s",
                token_id,
                source,
            )
            return None
        raw_timestamp = rows[0][0]
        if not isinstance(raw_timestamp, datetime):
            logger.info(
                "Latest history timestamp missing because row value is invalid "
                "token_id=%s source=%s raw_timestamp=%s",
                token_id,
                source,
                raw_timestamp,
            )
            return None
        if timezone.is_naive(raw_timestamp):
            logger.info(
                "Latest history timestamp was naive token_id=%s source=%s",
                token_id,
                source,
            )
            return timezone.make_aware(raw_timestamp, UTC)
        logger.info(
            "Loaded latest history timestamp token_id=%s source=%s timestamp=%s",
            token_id,
            source,
            raw_timestamp.astimezone(UTC).isoformat(),
        )
        return raw_timestamp.astimezone(UTC)

    def list_observations(
        self,
        *,
        market_external_id: str | None = None,
        token_id: str | None = None,
        limit: int = 100,
    ) -> list[PolymarketPriceInspectionRow]:
        logger.info(
            "Listing observations market_external_id=%s token_id=%s limit=%s",
            market_external_id,
            token_id,
            limit,
        )
        filters = ["1 = 1"]
        parameters: dict[str, object] = {"limit": limit}
        if market_external_id is not None:
            logger.info("Applying observation filter market_external_id=%s", market_external_id)
            filters.append("market_external_id = %(market_external_id)s")
            parameters["market_external_id"] = market_external_id
        if token_id is not None:
            logger.info("Applying observation filter token_id=%s", token_id)
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
        for index, row in enumerate(rows, start=1):
            observed_at, market_id, condition_id, row_token_id, side, price, source = row
            if not isinstance(observed_at, datetime):
                logger.info("Skipping observation row because observed_at is invalid row=%s", row)
                continue
            parsed_price = self._parse_price_decimal(price)
            if parsed_price is None:
                logger.info("Skipping observation row because price is invalid row=%s", row)
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
            if should_log_progress(index, every=100):
                logger.info(
                    "Collected observation rows progress processed=%s emitted=%s last_token_id=%s",
                    index,
                    len(observations),
                    row_token_id,
                )
        logger.info("Finished listing observations count=%s", len(observations))
        return observations

    def _parse_price_decimal(self, value: object) -> Decimal | None:
        if isinstance(value, Decimal):
            logger.info("Price decimal already Decimal value=%s", value)
            return value
        if isinstance(value, int | float | str):
            logger.info("Parsing price decimal value=%s", value)
            return Decimal(str(value))
        logger.info("Price decimal rejected unsupported value=%s", value)
        return None

    def _normalize_observed_at(self, observed_at: datetime) -> datetime:
        if timezone.is_naive(observed_at):
            logger.info("Observed timestamp was naive value=%s", observed_at.isoformat())
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
        logger.info(
            "Starting price sync "
            "batch_size=%s max_markets=%s fidelity_minutes=%s chunk_size_minutes=%s",
            batch_size,
            max_markets,
            fidelity_minutes,
            chunk_size_minutes,
        )
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
            logger.info(
                "Processing market batch size=%s cumulative_market_count=%s",
                len(market_batch),
                market_count,
            )
            for market in market_batch:
                for token_id in self._get_token_ids(market):
                    token_ids_seen.add(token_id)
                    logger.info(
                        "Syncing token history market_external_id=%s token_id=%s",
                        market.external_id,
                        token_id,
                    )
                    for resolution in resolutions:
                        price_count += self._sync_token_history(
                            market=market,
                            token_id=token_id,
                            resolution=resolution,
                            chunk_size_minutes=chunk_size_minutes,
                            single_resolution_mode=fidelity_minutes is not None,
                        )

        result = PolymarketPriceSyncResult(
            market_count=market_count,
            token_count=len(token_ids_seen),
            price_count=price_count,
        )
        logger.info(
            "Finished price sync markets=%s tokens=%s prices=%s",
            result.market_count,
            result.token_count,
            result.price_count,
        )
        return result

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
            logger.info(
                "Skipping token history because start is after end "
                "market_external_id=%s token_id=%s fidelity_minutes=%s "
                "start_timestamp=%s end_timestamp=%s",
                market.external_id,
                token_id,
                resolution.fidelity_minutes,
                start_timestamp.isoformat(),
                end_timestamp.isoformat(),
            )
            return 0

        inserted_count = 0
        current_start = start_timestamp
        chunk_delta = timedelta(minutes=chunk_size_minutes)
        chunk_index = 0

        while current_start <= end_timestamp:
            chunk_index += 1
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
            if should_log_progress(chunk_index, every=10):
                logger.info(
                    "Synced token history progress market_external_id=%s token_id=%s "
                    "fidelity_minutes=%s chunk_index=%s inserted_count=%s "
                    "current_start=%s current_end=%s",
                    market.external_id,
                    token_id,
                    resolution.fidelity_minutes,
                    chunk_index,
                    inserted_count,
                    current_start.isoformat(),
                    current_end.isoformat(),
                )
            current_start = current_end + timedelta(minutes=resolution.fidelity_minutes)

        logger.info(
            "Finished token history sync market_external_id=%s token_id=%s "
            "fidelity_minutes=%s inserted_count=%s",
            market.external_id,
            token_id,
            resolution.fidelity_minutes,
            inserted_count,
        )
        return inserted_count

    def _iter_market_batches(
        self,
        *,
        batch_size: int,
        max_markets: int | None,
    ) -> Iterable[list[PolymarketMarket]]:
        markets = PolymarketMarket.objects.filter(sync_prices=True).order_by("external_id")
        if max_markets is not None:
            logger.info("Applying max markets limit to price sync max_markets=%s", max_markets)
            markets = markets[:max_markets]

        batch: list[PolymarketMarket] = []
        request_count = 0
        for index, market in enumerate(markets, start=1):
            token_count = len(self._get_token_ids(market))
            if token_count == 0:
                logger.info("Skipping market with no token ids external_id=%s", market.external_id)
                continue
            if batch and request_count + token_count > batch_size:
                logger.info(
                    "Yielding market batch because batch_size would be exceeded "
                    "current_batch_size=%s request_count=%s next_token_count=%s",
                    len(batch),
                    request_count,
                    token_count,
                )
                yield batch
                batch = []
                request_count = 0
            batch.append(market)
            request_count += token_count
            if should_log_progress(index, every=100):
                logger.info(
                    "Built market batch progress "
                    "processed_markets=%s current_batch_size=%s request_count=%s",
                    index,
                    len(batch),
                    request_count,
                )

        if batch:
            logger.info(
                "Yielding final market batch size=%s request_count=%s",
                len(batch),
                request_count,
            )
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
            logger.info(
                "Using latest history timestamp for start "
                "market_external_id=%s token_id=%s fidelity_minutes=%s "
                "latest_timestamp=%s",
                market.external_id,
                token_id,
                resolution.fidelity_minutes,
                latest_timestamp.isoformat(),
            )
            return latest_timestamp + timedelta(minutes=resolution.fidelity_minutes)

        market_timestamp = market.market_created_at or market.start_date or timezone.now()
        if market.market_created_at is not None:
            logger.info(
                "Using market_created_at as history start external_id=%s",
                market.external_id,
            )
        elif market.start_date is not None:
            logger.info("Using start_date as history start external_id=%s", market.external_id)
        else:
            logger.info("Using current time as history start external_id=%s", market.external_id)
        normalized_market_timestamp = self._normalize_observed_at(market_timestamp)
        resolution_start = self._get_resolution_start_timestamp(resolution=resolution)
        if resolution_start is None:
            logger.info(
                "Using normalized market timestamp because resolution has no lookback "
                "external_id=%s timestamp=%s",
                market.external_id,
                normalized_market_timestamp.isoformat(),
            )
            return normalized_market_timestamp
        logger.info(
            "Using max of market timestamp and resolution start external_id=%s "
            "market_timestamp=%s resolution_start=%s",
            market.external_id,
            normalized_market_timestamp.isoformat(),
            resolution_start.isoformat(),
        )
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
            logger.info(
                "Using current time as history end because single resolution mode is enabled"
            )
            return now
        if resolution.lookback is None and self.default_resolutions:
            previous_resolution = self.default_resolutions[0]
            previous_resolution_start = self._get_resolution_start_timestamp(
                resolution=previous_resolution
            )
            if previous_resolution_start is None:
                logger.info(
                    "Using current time as history end because previous resolution start is missing"
                )
                return now
            logger.info(
                "Using previous resolution boundary as history end previous_resolution_start=%s",
                previous_resolution_start.isoformat(),
            )
            return previous_resolution_start - timedelta(minutes=1)
        logger.info("Using current time as history end because resolution has bounded lookback")
        return now

    def _get_resolution_start_timestamp(
        self,
        *,
        resolution: PolymarketPriceResolution,
    ) -> datetime | None:
        if resolution.lookback is None:
            logger.info(
                "Resolution start timestamp absent because lookback is None fidelity_minutes=%s",
                resolution.fidelity_minutes,
            )
            return None
        start_timestamp = timezone.now().astimezone(UTC) - resolution.lookback
        logger.info(
            "Computed resolution start timestamp fidelity_minutes=%s start_timestamp=%s",
            resolution.fidelity_minutes,
            start_timestamp.isoformat(),
        )
        return start_timestamp

    def _get_resolutions(
        self,
        fidelity_minutes: int | None,
    ) -> tuple[PolymarketPriceResolution, ...]:
        if fidelity_minutes is not None:
            logger.info("Using single price resolution fidelity_minutes=%s", fidelity_minutes)
            return (PolymarketPriceResolution(fidelity_minutes=fidelity_minutes, lookback=None),)
        logger.info("Using default mixed price resolutions count=%s", len(self.default_resolutions))
        return self.default_resolutions

    def _get_history_source(self, fidelity_minutes: int) -> str:
        return f"clob_prices_history_{fidelity_minutes}m"

    def _get_token_ids(self, market: PolymarketMarket) -> list[str]:
        token_ids = [
            str(value) for value in market.clob_token_ids if isinstance(value, str) and value
        ]
        if token_ids:
            logger.info(
                "Extracted token ids external_id=%s token_count=%s",
                market.external_id,
                len(token_ids),
            )
        else:
            logger.info("No valid token ids found external_id=%s", market.external_id)
        return token_ids

    def _normalize_observed_at(self, observed_at: datetime) -> datetime:
        return self.storage._normalize_observed_at(observed_at)
