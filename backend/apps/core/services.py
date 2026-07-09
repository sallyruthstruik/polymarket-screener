from pydantic import BaseModel, ConfigDict


class HealthStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str


class HealthService:
    def get_status(self) -> HealthStatus:
        return HealthStatus(status="ok")
