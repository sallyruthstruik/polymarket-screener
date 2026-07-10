import json
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

from apps.markets.types import JsonObject


class PolymarketGammaMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    created_at: datetime | None
    payload: JsonObject


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
