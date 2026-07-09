from dataclasses import dataclass


@dataclass(frozen=True)
class HealthStatus:
    status: str


class HealthService:
    def get_status(self) -> HealthStatus:
        return HealthStatus(status="ok")
