from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.markets.services.prices import PolymarketPriceSyncService


class Command(BaseCommand):
    help = "Sync prices for Polymarket markets with sync_prices enabled."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Maximum CLOB price requests per batch.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of markets to sync.",
        )
        parser.add_argument(
            "--fidelity-minutes",
            type=int,
            default=1,
            help="Price history fidelity in minutes.",
        )
        parser.add_argument(
            "--chunk-size-minutes",
            type=int,
            default=60 * 24,
            help="Time range per history request in minutes.",
        )

    def handle(self, *args: object, **options: object) -> str:
        batch_size = cast(int, options["batch_size"])
        limit = cast(int | None, options["limit"])
        fidelity_minutes = cast(int, options["fidelity_minutes"])
        chunk_size_minutes = cast(int, options["chunk_size_minutes"])

        if batch_size < 1:
            msg = "--batch-size must be greater than 0"
            raise CommandError(msg)
        if limit is not None and limit < 1:
            msg = "--limit must be greater than 0"
            raise CommandError(msg)
        if fidelity_minutes < 1:
            msg = "--fidelity-minutes must be greater than 0"
            raise CommandError(msg)
        if chunk_size_minutes < fidelity_minutes:
            msg = "--chunk-size-minutes must be greater than or equal to --fidelity-minutes"
            raise CommandError(msg)

        result = PolymarketPriceSyncService().sync_prices(
            batch_size=batch_size,
            max_markets=limit,
            fidelity_minutes=fidelity_minutes,
            chunk_size_minutes=chunk_size_minutes,
        )
        output = (
            "Synced Polymarket prices: "
            f"markets={result.market_count}, "
            f"tokens={result.token_count}, "
            f"prices={result.price_count}"
        )
        self.stdout.write(self.style.SUCCESS(output))
        return output
