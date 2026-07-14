from collections.abc import Sequence
from typing import Any, cast

from django.conf import settings
from pydantic import BaseModel, ConfigDict

from apps.core.logging import get_logger

logger = get_logger("apps.markets.services.clickhouse")


class ClickHouseSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str
    port: int
    database: str
    username: str
    password: str

    @classmethod
    def from_django_settings(cls) -> "ClickHouseSettings":
        raw_settings: dict[str, str] = settings.CLICKHOUSE
        return cls(
            host=raw_settings["HOST"],
            port=int(raw_settings["PORT"]),
            database=raw_settings["DATABASE"],
            username=raw_settings["USER"],
            password=raw_settings["PASSWORD"],
        )


class ClickHouseClient:
    def __init__(self, clickhouse_settings: ClickHouseSettings | None = None) -> None:
        import clickhouse_connect  # type: ignore[import-not-found]

        resolved_settings = clickhouse_settings or ClickHouseSettings.from_django_settings()
        logger.info(
            "Initializing ClickHouse client host=%s port=%s database=%s username=%s",
            resolved_settings.host,
            resolved_settings.port,
            resolved_settings.database,
            resolved_settings.username,
        )
        self._client: Any = clickhouse_connect.get_client(
            host=resolved_settings.host,
            port=resolved_settings.port,
            database=resolved_settings.database,
            username=resolved_settings.username,
            password=resolved_settings.password,
        )

    def command(self, query: str) -> None:
        logger.info("Executing ClickHouse command")
        self._client.command(query)

    def insert(
        self,
        table: str,
        rows: Sequence[Sequence[object]],
        column_names: Sequence[str],
    ) -> None:
        logger.info(
            "Executing ClickHouse insert table=%s row_count=%s column_count=%s",
            table,
            len(rows),
            len(column_names),
        )
        self._client.insert(table, rows, column_names=column_names)

    def query(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> Sequence[Sequence[object]]:
        logger.info(
            "Executing ClickHouse query has_parameters=%s",
            parameters is not None and bool(parameters),
        )
        result: Any = self._client.query(query, parameters=parameters)
        return cast(Sequence[Sequence[object]], result.result_rows)


def sql_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def sql_in_strings(values: Sequence[str]) -> str:
    if not values:
        return "('')"
    return f"({', '.join(sql_quote(value) for value in values)})"
