from __future__ import annotations

import logging
from datetime import UTC, datetime
from io import StringIO
from urllib.request import Request

from pytest import MonkeyPatch

from apps.markets.clients import polymarket
from apps.markets.clients.polymarket import PolymarketClobPriceClient, PolymarketGammaClient


class FakeHttpResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class BufferingHandler(logging.StreamHandler[StringIO]):
    def __init__(self) -> None:
        self.buffer = StringIO()
        super().__init__(self.buffer)


def test_gamma_client_logs_request_and_response_preview(
    monkeypatch: MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: int) -> FakeHttpResponse:
        assert timeout == 30
        assert "markets/keyset" in request.full_url
        return FakeHttpResponse('{"markets":[]}')

    monkeypatch.setattr(polymarket, "urlopen", fake_urlopen)
    handler = BufferingHandler()
    polymarket.logger.addHandler(handler)

    try:
        list(PolymarketGammaClient().iter_markets(closed=False))
    finally:
        polymarket.logger.removeHandler(handler)
    output = handler.buffer.getvalue()

    assert "Gamma request url=" in output
    assert 'Gamma response body_preview={"markets":[]}' in output
    assert "Stopping Gamma market iteration because page returned no markets" in output


def test_clob_client_logs_history_branching() -> None:
    client = PolymarketClobPriceClient()
    handler = BufferingHandler()
    polymarket.logger.addHandler(handler)

    try:
        client._parse_price_history_response(
            {
                "history": [
                    {"t": "bad", "p": "0.5"},
                    {"t": 1_783_680_000, "p": ""},
                ]
            },
            end_timestamp=datetime(2026, 7, 10, 12, 2, tzinfo=UTC),
        )
    finally:
        polymarket.logger.removeHandler(handler)
    output = handler.buffer.getvalue()

    assert "Skipping history point because timestamp is invalid" in output
    assert "Skipping history point because price is invalid" in output
    assert "Parsed history response point_count=0" in output
