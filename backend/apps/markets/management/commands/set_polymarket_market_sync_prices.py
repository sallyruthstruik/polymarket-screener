from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.markets.models import PolymarketMarket


class Command(BaseCommand):
    help = "Enable or disable sync_prices for Polymarket markets."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "external_ids",
            nargs="*",
            help="Market external ids to update.",
        )
        parser.add_argument(
            "--enabled",
            choices=("true", "false"),
            required=True,
            help="Whether sync_prices should be enabled for the selected markets.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Apply the change to all markets.",
        )

    def handle(self, *args: object, **options: object) -> str:
        external_ids = cast(list[str], options["external_ids"])
        enabled = cast(str, options["enabled"]) == "true"
        update_all = cast(bool, options["all"])

        if update_all and external_ids:
            msg = "Pass either external ids or --all, not both"
            raise CommandError(msg)
        if not update_all and not external_ids:
            msg = "Pass at least one external id or use --all"
            raise CommandError(msg)

        queryset = (
            PolymarketMarket.objects.all()
            if update_all
            else PolymarketMarket.objects.filter(external_id__in=external_ids)
        )
        updated_count = queryset.update(sync_prices=enabled)
        output = (
            "Updated Polymarket market price sync: "
            f"enabled={enabled}, "
            f"updated={updated_count}"
        )
        self.stdout.write(self.style.SUCCESS(output))
        return output
