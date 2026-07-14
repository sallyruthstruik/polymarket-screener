import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.core.logging import get_logger, should_log_progress
from apps.markets.clients.polymarket import PolymarketGammaClient, PolymarketGammaMarket
from apps.markets.models import PolymarketMarket
from apps.markets.services.clickhouse import ClickHouseClient
from apps.markets.types import JsonList, JsonObject

logger = get_logger("apps.markets.services.polymarket")



class PolymarketMarketData(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    condition_id: str
    slug: str
    question: str
    active: bool | None
    closed: bool | None
    archived: bool | None
    restricted: bool | None
    accepting_orders: bool | None
    market_created_at: datetime | None
    market_updated_at: datetime | None
    start_date: datetime | None
    end_date: datetime | None
    liquidity: Decimal | None
    volume: Decimal | None


class PolymarketMarketUpsertResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: PolymarketMarketData
    created: bool


class PolymarketMarketSyncResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    fetched_count: int
    created_count: int
    updated_count: int


class PolymarketMarketRawPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    synced_at: datetime
    market_external_id: str
    condition_id: str
    slug: str
    payload_json: str


class PolymarketMarketRawPayloadStorageService:
    table_name = "polymarket_market_raw_payloads"
    column_names = (
        "synced_at",
        "market_external_id",
        "condition_id",
        "slug",
        "payload_json",
    )

    def __init__(self, client: ClickHouseClient | None = None) -> None:
        self.client = client or ClickHouseClient()

    def ensure_table(self) -> None:
        logger.info("Ensuring raw payload ClickHouse table exists table=%s", self.table_name)
        self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name}
            (
                synced_at DateTime64(3, 'UTC'),
                market_external_id String,
                condition_id String,
                slug String,
                payload_json String
            )
            ENGINE = MergeTree()
            PARTITION BY toYYYYMM(synced_at)
            ORDER BY (market_external_id, synced_at)
            """
        )

    def insert_payload(self, market: PolymarketGammaMarket) -> None:
        logger.info("Inserting raw payload market_external_id=%s", market.external_id)
        row = PolymarketMarketRawPayload(
            synced_at=timezone.now().astimezone(UTC),
            market_external_id=market.external_id,
            condition_id=self._get_str(market.payload, "conditionId"),
            slug=self._get_str(market.payload, "slug"),
            payload_json=json.dumps(market.payload, separators=(",", ":"), sort_keys=True),
        )
        self.client.insert(
            self.table_name,
            [
                (
                    row.synced_at,
                    row.market_external_id,
                    row.condition_id,
                    row.slug,
                    row.payload_json,
                )
            ],
            self.column_names,
        )

    def list_payloads(
        self,
        *,
        market_external_id: str | None = None,
        limit: int = 100,
    ) -> list[PolymarketMarketRawPayload]:
        logger.info(
            "Listing raw payloads market_external_id=%s limit=%s",
            market_external_id,
            limit,
        )
        filters = ["1 = 1"]
        parameters: dict[str, object] = {"limit": limit}
        if market_external_id is not None:
            logger.info("Applying raw payload filter market_external_id=%s", market_external_id)
            filters.append("market_external_id = %(market_external_id)s")
            parameters["market_external_id"] = market_external_id

        rows = self.client.query(
            f"""
            SELECT synced_at, market_external_id, condition_id, slug, payload_json
            FROM {self.table_name}
            WHERE {' AND '.join(filters)}
            ORDER BY synced_at DESC
            LIMIT %(limit)s
            """,
            parameters=parameters,
        )
        payloads: list[PolymarketMarketRawPayload] = []
        for index, row in enumerate(rows, start=1):
            synced_at, row_market_external_id, condition_id, slug, payload_json = row
            if not isinstance(synced_at, datetime):
                logger.info("Skipping raw payload row because synced_at is invalid row=%s", row)
                continue
            payloads.append(
                PolymarketMarketRawPayload(
                    synced_at=self._normalize_synced_at(synced_at),
                    market_external_id=str(row_market_external_id),
                    condition_id=str(condition_id),
                    slug=str(slug),
                    payload_json=str(payload_json),
                )
            )
            if should_log_progress(index, every=100):
                logger.info(
                    "Collected raw payload rows progress "
                    "processed=%s emitted=%s last_market_external_id=%s",
                    index,
                    len(payloads),
                    row_market_external_id,
                )
        logger.info("Finished listing raw payloads count=%s", len(payloads))
        return payloads

    def _get_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    def _normalize_synced_at(self, synced_at: datetime) -> datetime:
        if timezone.is_naive(synced_at):
            return timezone.make_aware(synced_at, UTC)
        return synced_at.astimezone(UTC)


class PolymarketMarketStorageService:
    # Keep all Django ORM access behind this boundary so callers can keep using
    # Pydantic models if market storage moves away from Django tables later.
    @transaction.atomic
    def upsert_market(self, market: PolymarketGammaMarket) -> PolymarketMarketUpsertResult:
        logger.info("Upserting market external_id=%s", market.external_id)
        defaults = self._build_model_defaults(market.payload)
        instance, created = PolymarketMarket.objects.update_or_create(
            external_id=market.external_id,
            defaults=defaults,
        )
        if created:
            logger.info("Created market external_id=%s", market.external_id)
        else:
            logger.info("Updated market external_id=%s", market.external_id)
        return PolymarketMarketUpsertResult(
            market=self._to_data(instance),
            created=created,
        )

    def _build_model_defaults(self, payload: JsonObject) -> dict[str, object]:
        logger.info("Building market defaults payload_id=%s", payload.get("id"))
        return {
            "condition_id": self._get_str(payload, "conditionId"),
            "slug": self._get_str(payload, "slug"),
            "question": self._get_str(payload, "question"),
            "description": self._get_str(payload, "description"),
            "category": self._get_str(payload, "category"),
            "active": self._get_bool(payload, "active"),
            "closed": self._get_bool(payload, "closed"),
            "archived": self._get_bool(payload, "archived"),
            "restricted": self._get_bool(payload, "restricted"),
            "accepting_orders": self._get_bool(payload, "acceptingOrders"),
            "market_created_at": self._get_datetime(payload, "createdAt"),
            "market_updated_at": self._get_datetime(payload, "updatedAt"),
            "start_date": self._get_datetime(payload, "startDate"),
            "end_date": self._get_datetime(payload, "endDate"),
            "liquidity": self._get_decimal(payload, "liquidityNum", "liquidity"),
            "volume": self._get_decimal(payload, "volumeNum", "volume"),
            "liquidity_clob": self._get_decimal(payload, "liquidityClob"),
            "volume_clob": self._get_decimal(payload, "volumeClob"),
            "volume_24hr": self._get_decimal(payload, "volume24hr", "volume24hrClob"),
            "clob_token_ids": self._get_json_list(payload, "clobTokenIds"),
        }

    def _to_data(self, market: PolymarketMarket) -> PolymarketMarketData:
        return PolymarketMarketData(
            external_id=market.external_id,
            condition_id=market.condition_id,
            slug=market.slug,
            question=market.question,
            active=market.active,
            closed=market.closed,
            archived=market.archived,
            restricted=market.restricted,
            accepting_orders=market.accepting_orders,
            market_created_at=market.market_created_at,
            market_updated_at=market.market_updated_at,
            start_date=market.start_date,
            end_date=market.end_date,
            liquidity=market.liquidity,
            volume=market.volume,
        )

    def _get_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    def _get_bool(self, payload: JsonObject, key: str) -> bool | None:
        value = payload.get(key)
        if isinstance(value, bool):
            logger.info("Parsed boolean field key=%s value=%s", key, value)
        else:
            logger.info("Boolean field missing or invalid key=%s value=%s", key, value)
        return value if isinstance(value, bool) else None

    def _get_datetime(self, payload: JsonObject, key: str) -> datetime | None:
        value = payload.get(key)
        if not isinstance(value, str) or value == "":
            logger.info("Datetime field missing or invalid key=%s value=%s", key, value)
            return None

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            logger.info("Datetime field was naive key=%s", key)
            return timezone.make_aware(parsed, UTC)
        logger.info("Parsed datetime field key=%s value=%s", key, parsed.isoformat())
        return parsed

    def _get_decimal(self, payload: JsonObject, *keys: str) -> Decimal | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, int | float | str) and value != "":
                try:
                    logger.info("Parsed decimal field key=%s value=%s", key, value)
                    return Decimal(str(value))
                except InvalidOperation:
                    logger.info("Decimal field invalid key=%s value=%s", key, value)
                    return None
        logger.info("Decimal fields missing or empty keys=%s", keys)
        return None

    def _get_json_list(self, payload: JsonObject, key: str) -> JsonList:
        value = payload.get(key)
        if isinstance(value, list):
            logger.info("Parsed JSON list field from list key=%s size=%s", key, len(value))
            return value
        if isinstance(value, str) and value:
            parsed: object = json.loads(value)
            if isinstance(parsed, list):
                logger.info("Parsed JSON list field from string key=%s size=%s", key, len(parsed))
                return parsed
            logger.info(
                "JSON list string did not parse to list "
                "key=%s parsed_type=%s",
                key,
                type(parsed).__name__,
            )
        else:
            logger.info("JSON list field missing or invalid key=%s value=%s", key, value)
        return []


class PolymarketMarketSyncService:
    def __init__(
        self,
        *,
        client: PolymarketGammaClient | None = None,
        storage: PolymarketMarketStorageService | None = None,
        raw_payload_storage: PolymarketMarketRawPayloadStorageService | None = None,
    ) -> None:
        self.client = client or PolymarketGammaClient()
        self.storage = storage or PolymarketMarketStorageService()
        self.raw_payload_storage = raw_payload_storage or PolymarketMarketRawPayloadStorageService()

    def sync_markets(
        self,
        *,
        include_closed: bool,
        created_since: datetime | None = None,
        page_size: int = 500,
        max_markets: int | None = None,
    ) -> PolymarketMarketSyncResult:
        logger.info(
            "Starting market sync include_closed=%s created_since=%s page_size=%s max_markets=%s",
            include_closed,
            created_since.isoformat() if created_since is not None else None,
            page_size,
            max_markets,
        )
        created_count = 0
        updated_count = 0
        fetched_count = 0
        self.raw_payload_storage.ensure_table()

        for market in self._iter_markets(
            include_closed=include_closed,
            created_since=created_since,
            page_size=page_size,
            max_markets=max_markets,
        ):
            self.raw_payload_storage.insert_payload(market)
            upsert_result = self.storage.upsert_market(market)
            fetched_count += 1
            if upsert_result.created:
                created_count += 1
                logger.info(
                    "Market sync stored created market "
                    "fetched_count=%s created_count=%s external_id=%s",
                    fetched_count,
                    created_count,
                    market.external_id,
                )
            else:
                updated_count += 1
                logger.info(
                    "Market sync stored updated market "
                    "fetched_count=%s updated_count=%s external_id=%s",
                    fetched_count,
                    updated_count,
                    market.external_id,
                )
            if should_log_progress(fetched_count, every=100):
                logger.info(
                    "Market sync progress fetched=%s created=%s updated=%s",
                    fetched_count,
                    created_count,
                    updated_count,
                )

        result = PolymarketMarketSyncResult(
            fetched_count=fetched_count,
            created_count=created_count,
            updated_count=updated_count,
        )
        logger.info(
            "Finished market sync fetched=%s created=%s updated=%s",
            result.fetched_count,
            result.created_count,
            result.updated_count,
        )
        return result

    def _iter_markets(
        self,
        *,
        include_closed: bool,
        created_since: datetime | None,
        page_size: int,
        max_markets: int | None,
    ) -> Iterable[PolymarketGammaMarket]:
        remaining_markets = max_markets
        for closed in self._closed_filters(include_closed):
            if remaining_markets == 0:
                logger.info(
                    "Stopping market filter iteration because "
                    "remaining_markets reached zero"
                )
                return
            fetched_for_filter = 0
            logger.info(
                "Iterating market filter closed=%s remaining_markets=%s",
                closed,
                remaining_markets,
            )
            for market in self.client.iter_markets(
                closed=closed,
                created_since=created_since,
                page_size=page_size,
                max_markets=remaining_markets,
            ):
                fetched_for_filter += 1
                yield market

            if remaining_markets is not None:
                remaining_markets -= fetched_for_filter
                logger.info(
                    "Finished market filter closed=%s fetched_for_filter=%s remaining_markets=%s",
                    closed,
                    fetched_for_filter,
                    remaining_markets,
                )

    def _closed_filters(self, include_closed: bool) -> tuple[bool, ...]:
        if include_closed:
            logger.info("Using open and closed market filters")
            return (False, True)
        logger.info("Using only open market filter")
        return (False,)
