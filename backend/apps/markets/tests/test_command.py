from datetime import datetime
from io import StringIO
from typing import ClassVar

from django.core.management import call_command
from pytest import MonkeyPatch

from apps.markets.management.commands import sync_polymarket_markets, sync_polymarket_prices
from apps.markets.services.polymarket import PolymarketMarketSyncResult
from apps.markets.services.prices import PolymarketPriceSyncResult


class FakeSyncService:
    calls: ClassVar[list[dict[str, object]]] = []

    def sync_markets(
        self,
        *,
        include_closed: bool,
        created_since: datetime | None = None,
        page_size: int = 500,
        max_markets: int | None = None,
    ) -> PolymarketMarketSyncResult:
        self.calls.append(
            {
                "include_closed": include_closed,
                "created_since": created_since,
                "page_size": page_size,
                "max_markets": max_markets,
            }
        )
        return PolymarketMarketSyncResult(
            fetched_count=3,
            created_count=2,
            updated_count=1,
        )


class FakePriceSyncService:
    calls: ClassVar[list[dict[str, object]]] = []

    def sync_prices(
        self,
        *,
        batch_size: int = 500,
        max_markets: int | None = None,
    ) -> PolymarketPriceSyncResult:
        self.calls.append(
            {
                "batch_size": batch_size,
                "max_markets": max_markets,
            }
        )
        return PolymarketPriceSyncResult(
            market_count=2,
            token_count=4,
            price_count=8,
        )


def test_sync_polymarket_markets_command_uses_service_options(
    monkeypatch: MonkeyPatch,
) -> None:
    FakeSyncService.calls = []
    monkeypatch.setattr(sync_polymarket_markets, "PolymarketMarketSyncService", FakeSyncService)
    stdout = StringIO()

    call_command(
        "sync_polymarket_markets",
        "--include-closed",
        "--created-since",
        "2026-07-01T00:00:00Z",
        "--page-size",
        "2",
        "--limit",
        "3",
        stdout=stdout,
    )

    assert len(FakeSyncService.calls) == 1
    assert FakeSyncService.calls[0]["include_closed"] is True
    assert FakeSyncService.calls[0]["page_size"] == 2
    assert FakeSyncService.calls[0]["max_markets"] == 3
    assert "fetched=3, created=2, updated=1" in stdout.getvalue()


def test_sync_polymarket_prices_command_uses_service_options(
    monkeypatch: MonkeyPatch,
) -> None:
    FakePriceSyncService.calls = []
    monkeypatch.setattr(sync_polymarket_prices, "PolymarketPriceSyncService", FakePriceSyncService)
    stdout = StringIO()

    call_command(
        "sync_polymarket_prices",
        "--batch-size",
        "10",
        "--limit",
        "2",
        stdout=stdout,
    )

    assert len(FakePriceSyncService.calls) == 1
    assert FakePriceSyncService.calls[0]["batch_size"] == 10
    assert FakePriceSyncService.calls[0]["max_markets"] == 2
    assert "markets=2, tokens=4, prices=8" in stdout.getvalue()
