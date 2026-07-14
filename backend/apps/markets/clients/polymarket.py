import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

from apps.core.logging import get_log_preview, get_logger, should_log_progress
from apps.markets.types import JsonObject

logger = get_logger("apps.markets.clients.polymarket")


class PolymarketGammaMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    created_at: datetime | None
    payload: JsonObject


type PolymarketPriceSide = Literal["BUY", "SELL"]


class PolymarketPriceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    side: PolymarketPriceSide


class PolymarketTokenPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    side: PolymarketPriceSide
    price: Decimal


class PolymarketPriceHistoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    price: Decimal


class PolymarketGammaClient:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com") -> None:
        self.base_url = base_url.rstrip("/")

    def iter_markets(
        self,
        *,
        closed: bool,
        created_since: datetime | None = None,
        page_size: int = 500,
        max_markets: int | None = None,
    ) -> Iterator[PolymarketGammaMarket]:
        logger.info(
            "Starting Gamma market iteration "
            "closed=%s created_since=%s page_size=%s max_markets=%s",
            closed,
            created_since.isoformat() if created_since is not None else None,
            page_size,
            max_markets,
        )
        fetched_count = 0
        cursor: str | None = None

        while True:
            response = self._get_markets_page(closed=closed, page_size=page_size, cursor=cursor)
            markets = self._get_market_payloads(response)

            if not markets:
                logger.info(
                    "Stopping Gamma market iteration because page returned no markets "
                    "closed=%s cursor=%s fetched_count=%s",
                    closed,
                    cursor,
                    fetched_count,
                )
                return

            should_stop = False
            for payload in markets:
                market = self._parse_market(payload)
                if created_since is not None and market.created_at is not None:
                    if market.created_at < created_since:
                        logger.info(
                            "Stopping Gamma market iteration because market is older than "
                            "created_since external_id=%s created_at=%s created_since=%s",
                            market.external_id,
                            market.created_at.isoformat(),
                            created_since.isoformat(),
                        )
                        should_stop = True
                        break

                yield market
                fetched_count += 1
                if should_log_progress(fetched_count, every=100):
                    logger.info(
                        "Yielded Gamma markets progress "
                        "closed=%s fetched_count=%s last_external_id=%s",
                        closed,
                        fetched_count,
                        market.external_id,
                    )
                if max_markets is not None and fetched_count >= max_markets:
                    logger.info(
                        "Stopping Gamma market iteration because max_markets was reached "
                        "closed=%s fetched_count=%s",
                        closed,
                        fetched_count,
                    )
                    return

            if should_stop:
                logger.info(
                    "Gamma market iteration stopped by created_since guard "
                    "closed=%s fetched_count=%s",
                    closed,
                    fetched_count,
                )
                return

            cursor = self._get_optional_str(response, "next_cursor")
            if cursor is None:
                logger.info(
                    "Stopping Gamma market iteration because next_cursor is missing "
                    "closed=%s fetched_count=%s",
                    closed,
                    fetched_count,
                )
                return
            logger.info(
                "Continuing Gamma market iteration with next cursor "
                "closed=%s cursor=%s fetched_count=%s",
                closed,
                cursor,
                fetched_count,
            )

    def _get_markets_page(self, *, closed: bool, page_size: int, cursor: str | None) -> JsonObject:
        params = {
            "limit": str(page_size),
            "closed": str(closed).lower(),
            "order": "createdAt",
            "ascending": "false",
        }
        if cursor is not None:
            params["after_cursor"] = cursor

        request = Request(
            f"{self.base_url}/markets/keyset?{urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": "polymarket-screener/1.0"},
        )
        logger.info("Gamma request url=%s", request.full_url)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            logger.info(
                "Gamma response error status=%s body_preview=%s",
                error.code,
                get_log_preview(error_body),
            )
            raise
        logger.info("Gamma response body_preview=%s", get_log_preview(body))

        return self._load_json_object(body)

    def _parse_market(self, payload: JsonObject) -> PolymarketGammaMarket:
        external_id = self._get_required_str(payload, "id")
        created_at = self._get_optional_datetime(payload, "createdAt")
        return PolymarketGammaMarket(
            external_id=external_id,
            created_at=created_at,
            payload=payload,
        )

    def _get_market_payloads(self, response: JsonObject) -> list[JsonObject]:
        raw_markets = response.get("markets")
        if not isinstance(raw_markets, list):
            logger.info(
                "Gamma response missing markets list response_keys=%s",
                sorted(response.keys()),
            )
            msg = "Gamma markets response does not contain a markets list"
            raise ValueError(msg)

        markets: list[JsonObject] = []
        for raw_market in raw_markets:
            if not isinstance(raw_market, dict):
                logger.info(
                    "Gamma response contains non-object market raw_market_type=%s",
                    type(raw_market).__name__,
                )
                msg = "Gamma markets response contains a non-object market"
                raise ValueError(msg)
            markets.append(raw_market)
        logger.info("Parsed Gamma markets page market_count=%s", len(markets))
        return markets

    def _load_json_object(self, body: str) -> JsonObject:
        parsed: object = json.loads(body)
        if not isinstance(parsed, dict):
            logger.info(
                "Gamma response JSON root is not an object root_type=%s",
                type(parsed).__name__,
            )
            msg = "Gamma response is not a JSON object"
            raise ValueError(msg)
        return parsed

    def _get_required_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or value == "":
            logger.info("Gamma payload missing required string field key=%s", key)
            msg = f"Gamma market payload missing required string field: {key}"
            raise ValueError(msg)
        return value

    def _get_optional_str(self, payload: JsonObject, key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    def _get_optional_datetime(self, payload: JsonObject, key: str) -> datetime | None:
        value = self._get_optional_str(payload, key)
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PolymarketClobPriceClient:
    def __init__(self, base_url: str = "https://clob.polymarket.com") -> None:
        self.base_url = base_url.rstrip("/")

    def fetch_prices(self, requests: list[PolymarketPriceRequest]) -> list[PolymarketTokenPrice]:
        if not requests:
            logger.info("Skipping CLOB prices request because request list is empty")
            return []

        body = json.dumps(
            [{"token_id": request.token_id, "side": request.side} for request in requests]
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/prices",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "polymarket-screener/1.0",
            },
        )
        logger.info(
            "CLOB prices request url=%s request_preview=%s",
            request.full_url,
            get_log_preview(body.decode("utf-8")),
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            logger.info(
                "CLOB prices response error status=%s body_preview=%s",
                error.code,
                get_log_preview(error_body),
            )
            raise
        logger.info("CLOB prices response body_preview=%s", get_log_preview(response_body))

        return self._parse_prices_response(self._load_json_object(response_body), requests)

    def fetch_price_history(
        self,
        *,
        token_id: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        fidelity_minutes: int,
    ) -> list[PolymarketPriceHistoryPoint]:
        params = {
            "market": token_id,
            "startTs": str(int(start_timestamp.timestamp())),
            "endTs": str(int(end_timestamp.timestamp())),
            "fidelity": str(fidelity_minutes),
        }
        request = Request(
            f"{self.base_url}/prices-history?{urlencode(params)}",
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-screener/1.0",
            },
        )
        logger.info("CLOB prices-history request url=%s", request.full_url)
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            logger.info(
                "CLOB prices-history response error status=%s body_preview=%s",
                error.code,
                get_log_preview(error_body),
            )
            raise
        logger.info("CLOB prices-history response body_preview=%s", get_log_preview(response_body))

        return self._parse_price_history_response(
            self._load_json_object(response_body),
            end_timestamp=end_timestamp,
        )

    def _load_json_object(self, body: str) -> JsonObject:
        parsed: object = json.loads(body)
        if not isinstance(parsed, dict):
            logger.info(
                "CLOB response JSON root is not an object root_type=%s",
                type(parsed).__name__,
            )
            msg = "CLOB prices response is not a JSON object"
            raise ValueError(msg)
        return parsed

    def _parse_prices_response(
        self,
        payload: JsonObject,
        requests: list[PolymarketPriceRequest],
    ) -> list[PolymarketTokenPrice]:
        prices: list[PolymarketTokenPrice] = []
        for index, request in enumerate(requests, start=1):
            token_prices = payload.get(request.token_id)
            if not isinstance(token_prices, dict):
                logger.info(
                    "Skipping CLOB price because token entry is missing token_id=%s side=%s",
                    request.token_id,
                    request.side,
                )
                continue
            raw_price = token_prices.get(request.side)
            price = self._parse_decimal(raw_price)
            if price is None:
                logger.info(
                    "Skipping CLOB price because price is invalid token_id=%s side=%s raw_price=%s",
                    request.token_id,
                    request.side,
                    raw_price,
                )
                continue
            prices.append(
                PolymarketTokenPrice(
                    token_id=request.token_id,
                    side=request.side,
                    price=price,
                )
            )
            if should_log_progress(index, every=100):
                logger.info(
                    "Parsed CLOB prices progress processed=%s emitted=%s last_token_id=%s",
                    index,
                    len(prices),
                    request.token_id,
                )
        return prices

    def _parse_decimal(self, value: object) -> Decimal | None:
        if not isinstance(value, int | float | str) or value == "":
            logger.info("Decimal parse rejected unsupported value value=%s", value)
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            logger.info("Decimal parse failed invalid value=%s", value)
            return None

    def _parse_price_history_response(
        self,
        payload: JsonObject,
        *,
        end_timestamp: datetime,
    ) -> list[PolymarketPriceHistoryPoint]:
        raw_history = payload.get("history")
        if not isinstance(raw_history, list):
            logger.info(
                "CLOB prices-history response missing history list payload_keys=%s",
                sorted(payload.keys()),
            )
            msg = "CLOB prices-history response does not contain a history list"
            raise ValueError(msg)

        history: list[PolymarketPriceHistoryPoint] = []
        max_timestamp = end_timestamp.timestamp()
        for index, raw_point in enumerate(raw_history, start=1):
            if not isinstance(raw_point, dict):
                logger.info(
                    "CLOB prices-history response contains non-object history point raw_type=%s",
                    type(raw_point).__name__,
                )
                msg = "CLOB prices-history response contains a non-object history point"
                raise ValueError(msg)

            raw_timestamp = raw_point.get("t")
            raw_price = raw_point.get("p")
            if not isinstance(raw_timestamp, int | float):
                logger.info(
                    "Skipping history point because timestamp is invalid raw_point=%s",
                    raw_point,
                )
                continue
            price = self._parse_decimal(raw_price)
            if price is None:
                logger.info(
                    "Skipping history point because price is invalid raw_point=%s",
                    raw_point,
                )
                continue

            if float(raw_timestamp) > max_timestamp:
                logger.info(
                    "Skipping history point because timestamp is in the future "
                    "raw_timestamp=%s end_timestamp=%s",
                    raw_timestamp,
                    end_timestamp.isoformat(),
                )
                continue

            history.append(
                PolymarketPriceHistoryPoint(
                    timestamp=datetime.fromtimestamp(float(raw_timestamp), tz=UTC),
                    price=price,
                )
            )
            if should_log_progress(index, every=500):
                logger.info(
                    "Parsed history progress processed=%s emitted=%s last_timestamp=%s",
                    index,
                    len(history),
                    history[-1].timestamp.isoformat(),
                )
        logger.info("Parsed history response point_count=%s", len(history))
        return history
