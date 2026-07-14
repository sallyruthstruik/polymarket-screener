from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.core.logging import get_logger, should_log_progress
from apps.markets.clients.polymarket import PolymarketGammaClient, PolymarketGammaMarket
from apps.markets.services.clickhouse import ClickHouseClient, sql_in_strings, sql_quote
from apps.markets.types import JsonObject

logger = get_logger("apps.markets.services.polymarket")

type PolymarketWriteSource = Literal["sync", "admin"]


class PolymarketMarketImportedFields(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition_id: str
    slug: str
    question: str
    description: str
    category: str
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
    liquidity_clob: Decimal | None
    volume_clob: Decimal | None
    volume_24hr: Decimal | None
    clob_token_ids: list[str]


class PolymarketMarketData(PolymarketMarketImportedFields):
    external_id: str
    sync_prices: bool
    first_synced_at: datetime
    last_synced_at: datetime
    written_at: datetime
    row_version: int
    write_source: PolymarketWriteSource


class PolymarketMarketSyncResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    fetched_count: int
    created_count: int
    updated_count: int


class PolymarketMarketPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    markets: list[PolymarketMarketData]
    total_count: int


class PolymarketMarketListFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    search: str = ""
    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    sync_prices: bool | None = None


class PolymarketMarketAdminInput(PolymarketMarketImportedFields):
    sync_prices: bool


class PolymarketMarketRawPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    synced_at: datetime
    market_external_id: str
    condition_id: str
    slug: str
    payload_json: str


class PolymarketMarketVersionGenerator:
    _lock = threading.Lock()
    _last_version = 0

    def next_version(self) -> int:
        with self._lock:
            candidate = time.time_ns()
            if candidate <= self._last_version:
                candidate = self._last_version + 1
            self._last_version = candidate
            return candidate


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
        inserted_count = self.insert_payloads([market])
        logger.info(
            "Inserted single raw payload market_external_id=%s inserted_count=%s",
            market.external_id,
            inserted_count,
        )

    def insert_payloads(self, markets: Sequence[PolymarketGammaMarket]) -> int:
        if not markets:
            logger.info("Skipping raw payload insert because market list is empty")
            return 0
        synced_at = timezone.now().astimezone(UTC)
        rows: list[tuple[object, ...]] = []
        for index, market in enumerate(markets, start=1):
            row = PolymarketMarketRawPayload(
                synced_at=synced_at,
                market_external_id=market.external_id,
                condition_id=self._get_str(market.payload, "conditionId"),
                slug=self._get_str(market.payload, "slug"),
                payload_json=json.dumps(market.payload, separators=(",", ":"), sort_keys=True),
            )
            rows.append(
                (
                    row.synced_at,
                    row.market_external_id,
                    row.condition_id,
                    row.slug,
                    row.payload_json,
                )
            )
            if should_log_progress(index, every=500):
                logger.info(
                    "Prepared raw payload batch progress processed=%s last_external_id=%s",
                    index,
                    market.external_id,
                )
        self.client.insert(self.table_name, rows, self.column_names)
        logger.info("Inserted raw payload rows count=%s", len(rows))
        return len(rows)

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
                    "Collected raw payload rows progress processed=%s emitted=%s",
                    index,
                    len(payloads),
                )
        logger.info("Finished listing raw payloads count=%s", len(payloads))
        return payloads

    def _get_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        if isinstance(value, str):
            return value
        return ""

    def _normalize_synced_at(self, synced_at: datetime) -> datetime:
        if timezone.is_naive(synced_at):
            logger.info("Raw payload synced_at was naive")
            return timezone.make_aware(synced_at, UTC)
        return synced_at.astimezone(UTC)


class PolymarketMarketStorageService:
    table_name = "polymarket_markets"
    column_names = (
        "external_id",
        "condition_id",
        "slug",
        "question",
        "description",
        "category",
        "active",
        "closed",
        "archived",
        "restricted",
        "accepting_orders",
        "market_created_at",
        "market_updated_at",
        "start_date",
        "end_date",
        "liquidity",
        "volume",
        "liquidity_clob",
        "volume_clob",
        "volume_24hr",
        "clob_token_ids",
        "sync_prices",
        "first_synced_at",
        "last_synced_at",
        "written_at",
        "row_version",
        "write_source",
    )
    select_columns = ", ".join(column_names)

    def __init__(
        self,
        *,
        client: ClickHouseClient | None = None,
        version_generator: PolymarketMarketVersionGenerator | None = None,
    ) -> None:
        self.client = client or ClickHouseClient()
        self.version_generator = version_generator or PolymarketMarketVersionGenerator()

    def ensure_table(self) -> None:
        logger.info("Ensuring market ClickHouse table exists table=%s", self.table_name)
        self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name}
            (
                external_id String,
                condition_id String DEFAULT '',
                slug String DEFAULT '',
                question String CODEC(ZSTD(3)),
                description String CODEC(ZSTD(3)),
                category LowCardinality(String) DEFAULT '',
                active Nullable(Bool),
                closed Nullable(Bool),
                archived Nullable(Bool),
                restricted Nullable(Bool),
                accepting_orders Nullable(Bool),
                market_created_at Nullable(DateTime64(3, 'UTC')),
                market_updated_at Nullable(DateTime64(3, 'UTC')),
                start_date Nullable(DateTime64(3, 'UTC')),
                end_date Nullable(DateTime64(3, 'UTC')),
                liquidity Nullable(Decimal(38, 12)),
                volume Nullable(Decimal(38, 12)),
                liquidity_clob Nullable(Decimal(38, 12)),
                volume_clob Nullable(Decimal(38, 12)),
                volume_24hr Nullable(Decimal(38, 12)),
                clob_token_ids Array(String),
                sync_prices Bool DEFAULT false,
                first_synced_at DateTime64(3, 'UTC'),
                last_synced_at DateTime64(3, 'UTC'),
                written_at DateTime64(6, 'UTC'),
                row_version UInt64,
                write_source Enum8('sync' = 1, 'admin' = 2),
                INDEX condition_id_bloom condition_id TYPE bloom_filter(0.01) GRANULARITY 4,
                INDEX slug_bloom slug TYPE bloom_filter(0.01) GRANULARITY 4
            )
            ENGINE = ReplacingMergeTree(row_version)
            ORDER BY external_id
            """
        )

    def insert_markets(
        self, markets: Sequence[PolymarketGammaMarket]
    ) -> PolymarketMarketSyncResult:
        if not markets:
            logger.info("Skipping market insert because market list is empty")
            return PolymarketMarketSyncResult(
                fetched_count=0,
                created_count=0,
                updated_count=0,
            )
        existing_markets = self._get_existing_markets_map(
            [market.external_id for market in markets]
        )
        synced_at = timezone.now().astimezone(UTC)
        rows: list[tuple[object, ...]] = []
        created_count = 0
        updated_count = 0
        for index, market in enumerate(markets, start=1):
            existing_market = existing_markets.get(market.external_id)
            imported_fields = self._build_market_fields(market.payload)
            row = self._build_market_row(
                external_id=market.external_id,
                imported_fields=imported_fields,
                existing_market=existing_market,
                sync_prices=existing_market.sync_prices if existing_market is not None else False,
                first_synced_at=(
                    existing_market.first_synced_at if existing_market is not None else synced_at
                ),
                last_synced_at=synced_at,
                written_at=synced_at,
                write_source="sync",
            )
            rows.append(self._market_to_row(row))
            if existing_market is None:
                created_count += 1
            else:
                updated_count += 1
            if should_log_progress(index, every=1000):
                logger.info(
                    "Prepared market batch progress processed=%s created=%s updated=%s",
                    index,
                    created_count,
                    updated_count,
                )
        self.client.insert(self.table_name, rows, self.column_names)
        logger.info(
            "Inserted market batch fetched=%s created=%s updated=%s",
            len(markets),
            created_count,
            updated_count,
        )
        return PolymarketMarketSyncResult(
            fetched_count=len(markets),
            created_count=created_count,
            updated_count=updated_count,
        )

    def get_market(self, external_id: str) -> PolymarketMarketData | None:
        logger.info("Loading market external_id=%s", external_id)
        rows = self.client.query(
            f"""
            SELECT {self.select_columns}
            FROM {self.table_name} FINAL
            WHERE external_id = %(external_id)s
            LIMIT 1
            """,
            parameters={"external_id": external_id},
        )
        if not rows:
            logger.info("Market lookup returned no rows external_id=%s", external_id)
            return None
        return self._row_to_market(rows[0])

    def list_markets(
        self,
        *,
        filters: PolymarketMarketListFilters,
        page: int,
        page_size: int,
    ) -> PolymarketMarketPage:
        logger.info(
            "Listing markets "
            "search=%s active=%s closed=%s archived=%s sync_prices=%s page=%s page_size=%s",
            filters.search,
            filters.active,
            filters.closed,
            filters.archived,
            filters.sync_prices,
            page,
            page_size,
        )
        where_clause = self._build_market_where_clause(filters)
        offset = (page - 1) * page_size
        count_rows = self.client.query(
            f"""
            SELECT count()
            FROM {self.table_name} FINAL
            WHERE {where_clause}
            """
        )
        total_count = self._coerce_required_int(count_rows[0][0]) if count_rows else 0
        rows = self.client.query(
            f"""
            SELECT {self.select_columns}
            FROM {self.table_name} FINAL
            WHERE {where_clause}
            ORDER BY isNull(market_created_at) ASC, market_created_at DESC, external_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            parameters={"limit": page_size, "offset": offset},
        )
        markets = [self._row_to_market(row) for row in rows]
        logger.info("Finished listing markets emitted=%s total_count=%s", len(markets), total_count)
        return PolymarketMarketPage(markets=markets, total_count=total_count)

    def save_admin_edit(
        self,
        *,
        external_id: str,
        market_input: PolymarketMarketAdminInput,
    ) -> PolymarketMarketData:
        existing_market = self.get_market(external_id)
        if existing_market is None:
            logger.info("Admin edit failed because market is missing external_id=%s", external_id)
            msg = f"Unknown market: {external_id}"
            raise LookupError(msg)
        written_at = timezone.now().astimezone(UTC)
        updated_market = self._build_market_row(
            external_id=external_id,
            imported_fields=PolymarketMarketImportedFields.model_validate(
                market_input.model_dump(exclude={"sync_prices"})
            ),
            existing_market=existing_market,
            sync_prices=market_input.sync_prices,
            first_synced_at=existing_market.first_synced_at,
            last_synced_at=written_at,
            written_at=written_at,
            write_source="admin",
        )
        self.client.insert(
            self.table_name,
            [self._market_to_row(updated_market)],
            self.column_names,
        )
        logger.info("Saved admin edit external_id=%s", external_id)
        return updated_market

    def set_sync_prices(
        self,
        *,
        external_ids: Sequence[str] | None,
        enabled: bool,
        update_all: bool = False,
    ) -> int:
        logger.info(
            "Setting sync_prices enabled=%s update_all=%s external_id_count=%s",
            enabled,
            update_all,
            len(external_ids) if external_ids is not None else None,
        )
        updated_count = 0
        for batch in self._iter_markets_for_sync_price_update(
            external_ids=external_ids,
            update_all=update_all,
        ):
            rows: list[tuple[object, ...]] = []
            written_at = timezone.now().astimezone(UTC)
            for market in batch:
                if market.sync_prices == enabled:
                    logger.info(
                        "Skipping sync_prices rewrite because value already matches external_id=%s",
                        market.external_id,
                    )
                    continue
                updated_market = market.model_copy(
                    update={
                        "sync_prices": enabled,
                        "last_synced_at": written_at,
                        "written_at": written_at,
                        "row_version": self.version_generator.next_version(),
                        "write_source": "admin",
                    }
                )
                rows.append(self._market_to_row(updated_market))
            if not rows:
                logger.info("Skipping sync_prices batch because it produced no replacement rows")
                continue
            self.client.insert(self.table_name, rows, self.column_names)
            updated_count += len(rows)
            logger.info(
                "Inserted sync_prices replacement batch row_count=%s updated_count=%s",
                len(rows),
                updated_count,
            )
        logger.info("Finished setting sync_prices updated_count=%s", updated_count)
        return updated_count

    def list_price_sync_markets(
        self,
        *,
        limit: int,
        offset: int,
    ) -> list[PolymarketMarketData]:
        logger.info("Listing price sync markets limit=%s offset=%s", limit, offset)
        rows = self.client.query(
            f"""
            SELECT {self.select_columns}
            FROM {self.table_name} FINAL
            WHERE sync_prices = true
            ORDER BY external_id ASC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            parameters={"limit": limit, "offset": offset},
        )
        markets = [self._row_to_market(row) for row in rows]
        logger.info("Finished listing price sync markets count=%s", len(markets))
        return markets

    def explain_market_lookup(
        self, *, field_name: Literal["external_id", "condition_id", "slug"]
    ) -> str:
        if field_name == "external_id":
            where_clause = "external_id = 'market-1'"
        elif field_name == "condition_id":
            where_clause = "condition_id = 'condition-1'"
        else:
            where_clause = "slug = 'slug-1'"
        logger.info("Explaining market lookup field_name=%s", field_name)
        rows = self.client.query(
            f"""
            EXPLAIN indexes = 1
            SELECT external_id
            FROM {self.table_name} FINAL
            WHERE {where_clause}
            """
        )
        return "\n".join(" ".join(str(value) for value in row) for row in rows)

    def _iter_markets_for_sync_price_update(
        self,
        *,
        external_ids: Sequence[str] | None,
        update_all: bool,
    ) -> Iterable[list[PolymarketMarketData]]:
        if update_all:
            logger.info("Iterating all markets for sync price update")
            offset = 0
            while True:
                batch = self.list_markets(
                    filters=PolymarketMarketListFilters(),
                    page=(offset // 500) + 1,
                    page_size=500,
                ).markets
                if not batch:
                    logger.info("Stopping sync price update iteration because batch is empty")
                    return
                yield batch
                offset += len(batch)
                if len(batch) < 500:
                    logger.info("Stopping sync price update iteration after partial batch")
                    return
        else:
            requested_ids = list(external_ids or [])
            if not requested_ids:
                logger.info("Skipping sync price update iteration because external_ids are empty")
                return
            current_markets = self._get_existing_markets_map(requested_ids)
            yield [
                current_markets[external_id]
                for external_id in requested_ids
                if external_id in current_markets
            ]

    def _get_existing_markets_map(
        self, external_ids: Sequence[str]
    ) -> dict[str, PolymarketMarketData]:
        unique_external_ids = list(dict.fromkeys(external_ids))
        if not unique_external_ids:
            logger.info("Skipping existing market lookup because external_ids are empty")
            return {}
        query = f"""
            SELECT {self.select_columns}
            FROM {self.table_name} FINAL
            WHERE external_id IN {sql_in_strings(unique_external_ids)}
        """
        rows = self.client.query(query)
        markets = [self._row_to_market(row) for row in rows]
        logger.info("Loaded existing market map count=%s", len(markets))
        return {market.external_id: market for market in markets}

    def _build_market_fields(self, payload: JsonObject) -> PolymarketMarketImportedFields:
        logger.info("Building market fields payload_id=%s", payload.get("id"))
        return PolymarketMarketImportedFields(
            condition_id=self._get_str(payload, "conditionId"),
            slug=self._get_str(payload, "slug"),
            question=self._get_str(payload, "question"),
            description=self._get_str(payload, "description"),
            category=self._get_str(payload, "category"),
            active=self._get_bool(payload, "active"),
            closed=self._get_bool(payload, "closed"),
            archived=self._get_bool(payload, "archived"),
            restricted=self._get_bool(payload, "restricted"),
            accepting_orders=self._get_bool(payload, "acceptingOrders"),
            market_created_at=self._get_datetime(payload, "createdAt"),
            market_updated_at=self._get_datetime(payload, "updatedAt"),
            start_date=self._get_datetime(payload, "startDate"),
            end_date=self._get_datetime(payload, "endDate"),
            liquidity=self._get_decimal(payload, "liquidityNum", "liquidity"),
            volume=self._get_decimal(payload, "volumeNum", "volume"),
            liquidity_clob=self._get_decimal(payload, "liquidityClob"),
            volume_clob=self._get_decimal(payload, "volumeClob"),
            volume_24hr=self._get_decimal(payload, "volume24hr", "volume24hrClob"),
            clob_token_ids=self._get_string_list(payload, "clobTokenIds"),
        )

    def _build_market_row(
        self,
        *,
        external_id: str,
        imported_fields: PolymarketMarketImportedFields,
        existing_market: PolymarketMarketData | None,
        sync_prices: bool,
        first_synced_at: datetime,
        last_synced_at: datetime,
        written_at: datetime,
        write_source: PolymarketWriteSource,
    ) -> PolymarketMarketData:
        if existing_market is None:
            logger.info("Building new market replacement row external_id=%s", external_id)
        else:
            logger.info("Building updated market replacement row external_id=%s", external_id)
        return PolymarketMarketData(
            external_id=external_id,
            sync_prices=sync_prices,
            first_synced_at=self._normalize_datetime(first_synced_at),
            last_synced_at=self._normalize_datetime(last_synced_at),
            written_at=self._normalize_datetime(written_at),
            row_version=self.version_generator.next_version(),
            write_source=write_source,
            **imported_fields.model_dump(),
        )

    def _row_to_market(self, row: Sequence[object]) -> PolymarketMarketData:
        (
            external_id,
            condition_id,
            slug,
            question,
            description,
            category,
            active,
            closed,
            archived,
            restricted,
            accepting_orders,
            market_created_at,
            market_updated_at,
            start_date,
            end_date,
            liquidity,
            volume,
            liquidity_clob,
            volume_clob,
            volume_24hr,
            clob_token_ids,
            sync_prices,
            first_synced_at,
            last_synced_at,
            written_at,
            row_version,
            write_source,
        ) = row
        return PolymarketMarketData(
            external_id=str(external_id),
            condition_id=str(condition_id),
            slug=str(slug),
            question=str(question),
            description=str(description),
            category=str(category),
            active=self._coerce_optional_bool(active),
            closed=self._coerce_optional_bool(closed),
            archived=self._coerce_optional_bool(archived),
            restricted=self._coerce_optional_bool(restricted),
            accepting_orders=self._coerce_optional_bool(accepting_orders),
            market_created_at=self._coerce_optional_datetime(market_created_at),
            market_updated_at=self._coerce_optional_datetime(market_updated_at),
            start_date=self._coerce_optional_datetime(start_date),
            end_date=self._coerce_optional_datetime(end_date),
            liquidity=self._coerce_optional_decimal(liquidity),
            volume=self._coerce_optional_decimal(volume),
            liquidity_clob=self._coerce_optional_decimal(liquidity_clob),
            volume_clob=self._coerce_optional_decimal(volume_clob),
            volume_24hr=self._coerce_optional_decimal(volume_24hr),
            clob_token_ids=self._coerce_string_list(clob_token_ids),
            sync_prices=bool(sync_prices),
            first_synced_at=self._coerce_required_datetime(first_synced_at),
            last_synced_at=self._coerce_required_datetime(last_synced_at),
            written_at=self._coerce_required_datetime(written_at),
            row_version=self._coerce_required_int(row_version),
            write_source=self._coerce_write_source(write_source),
        )

    def _market_to_row(self, market: PolymarketMarketData) -> tuple[object, ...]:
        return (
            market.external_id,
            market.condition_id,
            market.slug,
            market.question,
            market.description,
            market.category,
            market.active,
            market.closed,
            market.archived,
            market.restricted,
            market.accepting_orders,
            market.market_created_at,
            market.market_updated_at,
            market.start_date,
            market.end_date,
            market.liquidity,
            market.volume,
            market.liquidity_clob,
            market.volume_clob,
            market.volume_24hr,
            market.clob_token_ids,
            market.sync_prices,
            market.first_synced_at,
            market.last_synced_at,
            market.written_at,
            market.row_version,
            market.write_source,
        )

    def _build_market_where_clause(self, filters: PolymarketMarketListFilters) -> str:
        clauses = ["1 = 1"]
        if filters.search != "":
            logger.info("Applying market search filter search=%s", filters.search)
            exact = sql_quote(filters.search)
            contains = sql_quote(filters.search.lower())
            clauses.append(
                "("
                f"external_id = {exact} OR "
                f"condition_id = {exact} OR "
                f"slug = {exact} OR "
                f"positionCaseInsensitiveUTF8(question, {contains}) > 0"
                ")"
            )
        if filters.active is not None:
            logger.info("Applying active filter active=%s", filters.active)
            clauses.append(f"active = {self._sql_bool(filters.active)}")
        if filters.closed is not None:
            logger.info("Applying closed filter closed=%s", filters.closed)
            clauses.append(f"closed = {self._sql_bool(filters.closed)}")
        if filters.archived is not None:
            logger.info("Applying archived filter archived=%s", filters.archived)
            clauses.append(f"archived = {self._sql_bool(filters.archived)}")
        if filters.sync_prices is not None:
            logger.info("Applying sync_prices filter sync_prices=%s", filters.sync_prices)
            clauses.append(f"sync_prices = {self._sql_bool(filters.sync_prices)}")
        return " AND ".join(clauses)

    def _get_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        if isinstance(value, str):
            return value
        logger.info("String field missing or invalid key=%s value=%s", key, value)
        return ""

    def _get_bool(self, payload: JsonObject, key: str) -> bool | None:
        value = payload.get(key)
        if isinstance(value, bool):
            logger.info("Parsed boolean field key=%s value=%s", key, value)
            return value
        logger.info("Boolean field missing or invalid key=%s value=%s", key, value)
        return None

    def _get_datetime(self, payload: JsonObject, key: str) -> datetime | None:
        value = payload.get(key)
        if not isinstance(value, str) or value == "":
            logger.info("Datetime field missing or invalid key=%s value=%s", key, value)
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return self._normalize_datetime(parsed)

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

    def _get_string_list(self, payload: JsonObject, key: str) -> list[str]:
        value = payload.get(key)
        if isinstance(value, list):
            token_ids = [item for item in value if isinstance(item, str)]
            logger.info("Parsed string list from list key=%s size=%s", key, len(token_ids))
            return token_ids
        if isinstance(value, str) and value != "":
            parsed: object = json.loads(value)
            if isinstance(parsed, list):
                token_ids = [item for item in parsed if isinstance(item, str)]
                logger.info(
                    "Parsed string list from JSON string key=%s size=%s", key, len(token_ids)
                )
                return token_ids
            logger.info("JSON string field did not parse to list key=%s", key)
            return []
        logger.info("String list field missing or invalid key=%s value=%s", key, value)
        return []

    def _coerce_optional_bool(self, value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        logger.info("Optional boolean coercion fell back to None value=%s", value)
        return None

    def _coerce_optional_datetime(self, value: object) -> datetime | None:
        if isinstance(value, datetime):
            return self._normalize_datetime(value)
        if value is None:
            return None
        logger.info("Optional datetime coercion fell back to None value=%s", value)
        return None

    def _coerce_required_datetime(self, value: object) -> datetime:
        if not isinstance(value, datetime):
            logger.info("Required datetime coercion failed value=%s", value)
            msg = "Expected datetime value from ClickHouse row"
            raise ValueError(msg)
        return self._normalize_datetime(value)

    def _coerce_optional_decimal(self, value: object) -> Decimal | None:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int | float | str):
            return Decimal(str(value))
        if value is None:
            return None
        logger.info("Optional decimal coercion fell back to None value=%s", value)
        return None

    def _coerce_string_list(self, value: object) -> list[str]:
        if isinstance(value, list):
            token_ids = [item for item in value if isinstance(item, str)]
            logger.info("Coerced string list size=%s", len(token_ids))
            return token_ids
        logger.info("String list coercion fell back to empty list value=%s", value)
        return []

    def _coerce_write_source(self, value: object) -> PolymarketWriteSource:
        parsed = str(value)
        if parsed == "sync":
            return "sync"
        if parsed == "admin":
            return "admin"
        logger.info("Write source coercion failed value=%s", value)
        msg = f"Unexpected write_source value: {value}"
        raise ValueError(msg)

    def _normalize_datetime(self, value: datetime) -> datetime:
        if timezone.is_naive(value):
            logger.info("Normalizing naive datetime value=%s", value.isoformat())
            return timezone.make_aware(value, UTC)
        return value.astimezone(UTC)

    def _coerce_required_int(self, value: object) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        logger.info("Required int coercion failed value=%s", value)
        msg = "Expected integer value from ClickHouse row"
        raise ValueError(msg)

    def _sql_bool(self, value: bool) -> str:
        if value:
            return "true"
        return "false"


class PolymarketMarketSyncService:
    market_batch_size = 10_000
    raw_payload_batch_size = 1_000
    raw_payload_batch_bytes = 10 * 1024 * 1024

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
        self.storage.ensure_table()
        self.raw_payload_storage.ensure_table()

        fetched_count = 0
        created_count = 0
        updated_count = 0
        market_batch: list[PolymarketGammaMarket] = []
        raw_payload_batch: list[PolymarketGammaMarket] = []
        raw_payload_bytes = 0

        for market in self._iter_markets(
            include_closed=include_closed,
            created_since=created_since,
            page_size=page_size,
            max_markets=max_markets,
        ):
            market_batch.append(market)
            raw_payload_batch.append(market)
            raw_payload_bytes += len(
                json.dumps(market.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            if len(market_batch) >= self.market_batch_size:
                batch_result = self.storage.insert_markets(market_batch)
                fetched_count += batch_result.fetched_count
                created_count += batch_result.created_count
                updated_count += batch_result.updated_count
                market_batch = []
            if (
                len(raw_payload_batch) >= self.raw_payload_batch_size
                or raw_payload_bytes >= self.raw_payload_batch_bytes
            ):
                logger.info(
                    "Flushing raw payload batch row_count=%s approx_bytes=%s",
                    len(raw_payload_batch),
                    raw_payload_bytes,
                )
                self.raw_payload_storage.insert_payloads(raw_payload_batch)
                raw_payload_batch = []
                raw_payload_bytes = 0
            if should_log_progress(fetched_count + len(market_batch), every=100):
                logger.info(
                    "Market sync progress staged=%s created=%s updated=%s",
                    fetched_count + len(market_batch),
                    created_count,
                    updated_count,
                )

        if market_batch:
            logger.info("Flushing final market batch row_count=%s", len(market_batch))
            batch_result = self.storage.insert_markets(market_batch)
            fetched_count += batch_result.fetched_count
            created_count += batch_result.created_count
            updated_count += batch_result.updated_count
        if raw_payload_batch:
            logger.info("Flushing final raw payload batch row_count=%s", len(raw_payload_batch))
            self.raw_payload_storage.insert_payloads(raw_payload_batch)

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
                    "Stopping market filter iteration because remaining_markets reached zero"
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
