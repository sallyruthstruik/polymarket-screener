from collections.abc import Sequence
from typing import Any, cast

import clickhouse_connect
from django.conf import settings
from pydantic import BaseModel, ConfigDict


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
        resolved_settings = clickhouse_settings or ClickHouseSettings.from_django_settings()
        self._client: Any = clickhouse_connect.get_client(
            host=resolved_settings.host,
            port=resolved_settings.port,
            database=resolved_settings.database,
            username=resolved_settings.username,
            password=resolved_settings.password,
        )

    def command(self, query: str) -> None:
        self._client.command(query)

    def insert(
        self,
        table: str,
        rows: Sequence[Sequence[object]],
        column_names: Sequence[str],
    ) -> None:
        self._client.insert(table, rows, column_names=column_names)

    def query(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> Sequence[Sequence[object]]:
        result: Any = self._client.query(query, parameters=parameters)
        return cast(Sequence[Sequence[object]], result.result_rows)
