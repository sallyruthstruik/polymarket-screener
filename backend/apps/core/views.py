from django.http import JsonResponse

from apps.core.services import HealthService


def health_check(_request: object) -> JsonResponse:
    health = HealthService().get_status()
    return JsonResponse({"status": health.status})
