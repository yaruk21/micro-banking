from core.metrics import record_http_request, time_monotonic


class MetricsMiddleware:
    """Record bounded HTTP metrics for Prometheus-style exposition."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == "/metrics/":
            return self.get_response(request)

        started_at = time_monotonic()
        response = self.get_response(request)
        record_http_request(
            method=request.method,
            path=request.path_info,
            status_code=response.status_code,
            duration_seconds=time_monotonic() - started_at,
        )
        return response
