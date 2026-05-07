from django.http import HttpResponse, JsonResponse

from .metrics import render_metrics


def health_check(request):
    """Handle health check."""
    return JsonResponse({"status": "ok"})


def metrics_view(request):
    """Expose application metrics in Prometheus text format."""

    return HttpResponse(
        render_metrics(),
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )
