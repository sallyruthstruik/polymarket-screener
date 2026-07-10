import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

from apps.markets.types import JsonObject


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
        fetched_count = 0
        cursor: str | None = None

        while True:
            response = self._get_markets_page(closed=closed, page_size=page_size, cursor=cursor)
            markets = self._get_market_payloads(response)

            if not markets:
                return

            should_stop = False
            for payload in markets:
                market = self._parse_market(payload)
                if created_since is not None and market.created_at is not None:
                    if market.created_at < created_since:
                        should_stop = True
                        break

                yield market
                fetched_count += 1
                if max_markets is not None and fetched_count >= max_markets:
                    return

            if should_stop:
                return

            cursor = self._get_optional_str(response, "next_cursor")
            if cursor is None:
                return

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
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")

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
            msg = "Gamma markets response does not contain a markets list"
            raise ValueError(msg)

        markets: list[JsonObject] = []
        for raw_market in raw_markets:
            if not isinstance(raw_market, dict):
                msg = "Gamma markets response contains a non-object market"
                raise ValueError(msg)
            markets.append(raw_market)
        return markets

    def _load_json_object(self, body: str) -> JsonObject:
        parsed: object = json.loads(body)
        if not isinstance(parsed, dict):
            msg = "Gamma response is not a JSON object"
            raise ValueError(msg)
        return parsed

    def _get_required_str(self, payload: JsonObject, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or value == "":
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
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")

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
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")

        return self._parse_price_history_response(
            self._load_json_object(response_body),
            end_timestamp=end_timestamp,
        )

    def _load_json_object(self, body: str) -> JsonObject:
        parsed: object = json.loads(body)
        if not isinstance(parsed, dict):
            msg = "CLOB prices response is not a JSON object"
            raise ValueError(msg)
        return parsed

    def _parse_prices_response(
        self,
        payload: JsonObject,
        requests: list[PolymarketPriceRequest],
    ) -> list[PolymarketTokenPrice]:
        prices: list[PolymarketTokenPrice] = []
        for request in requests:
            token_prices = payload.get(request.token_id)
            if not isinstance(token_prices, dict):
                continue
            raw_price = token_prices.get(request.side)
            price = self._parse_decimal(raw_price)
            if price is None:
                continue
            prices.append(
                PolymarketTokenPrice(
                    token_id=request.token_id,
                    side=request.side,
                    price=price,
                )
            )
        return prices

    def _parse_decimal(self, value: object) -> Decimal | None:
        if not isinstance(value, int | float | str) or value == "":
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def _parse_price_history_response(
        self,
        payload: JsonObject,
        *,
        end_timestamp: datetime,
    ) -> list[PolymarketPriceHistoryPoint]:
        raw_history = payload.get("history")
        if not isinstance(raw_history, list):
            msg = "CLOB prices-history response does not contain a history list"
            raise ValueError(msg)

        history: list[PolymarketPriceHistoryPoint] = []
        max_timestamp = end_timestamp.timestamp()
        for raw_point in raw_history:
            if not isinstance(raw_point, dict):
                msg = "CLOB prices-history response contains a non-object history point"
                raise ValueError(msg)

            raw_timestamp = raw_point.get("t")
            raw_price = raw_point.get("p")
            if not isinstance(raw_timestamp, int | float):
                continue
            price = self._parse_decimal(raw_price)
            if price is None:
                continue

            if float(raw_timestamp) > max_timestamp:
                continue

            history.append(
                PolymarketPriceHistoryPoint(
                    timestamp=datetime.fromtimestamp(float(raw_timestamp), tz=UTC),
                    price=price,
                )
            )
        return history
