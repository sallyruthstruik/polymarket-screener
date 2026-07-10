from datetime import UTC, datetime
from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.markets.services import PolymarketMarketSyncService


class Command(BaseCommand):
    help = "Sync Polymarket markets from the Gamma API."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--created-since",
            dest="created_since",
            help="Only sync markets created at or after this ISO datetime.",
        )
        parser.add_argument(
            "--include-closed",
            action="store_true",
            help="Also sync closed historical markets.",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=500,
            help="Gamma keyset page size.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of markets to fetch.",
        )

    def handle(self, *args: object, **options: object) -> str:
        created_since = self._parse_created_since(cast(str | None, options["created_since"]))
        page_size = cast(int, options["page_size"])
        limit = cast(int | None, options["limit"])
        include_closed = cast(bool, options["include_closed"])

        if page_size < 1:
            msg = "--page-size must be greater than 0"
            raise CommandError(msg)
        if limit is not None and limit < 1:
            msg = "--limit must be greater than 0"
            raise CommandError(msg)

        result = PolymarketMarketSyncService().sync_markets(
            include_closed=include_closed,
            created_since=created_since,
            page_size=page_size,
            max_markets=limit,
        )
        output = (
            "Synced Polymarket markets: "
            f"fetched={result.fetched_count}, "
            f"created={result.created_count}, "
            f"updated={result.updated_count}"
        )
        self.stdout.write(self.style.SUCCESS(output))
        return output

    def _parse_created_since(self, value: str | None) -> datetime | None:
        if value is None:
            return None

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, UTC)
        return parsed
