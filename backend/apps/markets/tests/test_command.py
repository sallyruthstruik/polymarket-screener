from datetime import datetime
from io import StringIO
from typing import ClassVar

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import MonkeyPatch

from apps.markets.management.commands import (
    sync_polymarket_markets,
    sync_polymarket_prices,
)
from apps.markets.models import PolymarketMarket
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
        fidelity_minutes: int | None = None,
        chunk_size_minutes: int = 60 * 24,
    ) -> PolymarketPriceSyncResult:
        self.calls.append(
            {
                "batch_size": batch_size,
                "max_markets": max_markets,
                "fidelity_minutes": fidelity_minutes,
                "chunk_size_minutes": chunk_size_minutes,
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
        "--chunk-size-minutes",
        "1440",
        "--limit",
        "2",
        stdout=stdout,
    )

    assert len(FakePriceSyncService.calls) == 1
    assert FakePriceSyncService.calls[0]["batch_size"] == 10
    assert FakePriceSyncService.calls[0]["max_markets"] == 2
    assert FakePriceSyncService.calls[0]["fidelity_minutes"] is None
    assert FakePriceSyncService.calls[0]["chunk_size_minutes"] == 1440
    assert "markets=2, tokens=4, prices=8" in stdout.getvalue()


def test_set_polymarket_market_sync_prices_updates_selected_markets(db: None) -> None:
    enabled_market = _create_market(external_id="1", sync_prices=False)
    untouched_market = _create_market(external_id="2", sync_prices=False)
    stdout = StringIO()

    call_command(
        "set_polymarket_market_sync_prices",
        "1",
        "--enabled",
        "true",
        stdout=stdout,
    )

    enabled_market.refresh_from_db()
    untouched_market.refresh_from_db()
    assert enabled_market.sync_prices is True
    assert untouched_market.sync_prices is False
    assert "enabled=True, updated=1" in stdout.getvalue()


def test_set_polymarket_market_sync_prices_updates_all_markets(db: None) -> None:
    first_market = _create_market(external_id="1", sync_prices=False)
    second_market = _create_market(external_id="2", sync_prices=False)
    stdout = StringIO()

    call_command(
        "set_polymarket_market_sync_prices",
        "--all",
        "--enabled",
        "true",
        stdout=stdout,
    )

    first_market.refresh_from_db()
    second_market.refresh_from_db()
    assert first_market.sync_prices is True
    assert second_market.sync_prices is True
    assert "enabled=True, updated=2" in stdout.getvalue()


def test_set_polymarket_market_sync_prices_rejects_invalid_selection() -> None:
    with pytest.raises(CommandError, match="Pass either external ids or --all, not both"):
        call_command(
            "set_polymarket_market_sync_prices",
            "1",
            "--all",
            "--enabled",
            "true",
        )


def _create_market(*, external_id: str, sync_prices: bool) -> PolymarketMarket:
    return PolymarketMarket.objects.create(
        external_id=external_id,
        condition_id=f"condition-{external_id}",
        slug=f"market-{external_id}",
        question=f"Market {external_id}",
        active=True,
        closed=False,
        archived=False,
        restricted=False,
        accepting_orders=True,
        clob_token_ids=[f"token-{external_id}-yes", f"token-{external_id}-no"],
        sync_prices=sync_prices,
        raw_payload={},
    )
