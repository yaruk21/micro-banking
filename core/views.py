from django.http import JsonResponse


def health_check(request):
    """Handle health check."""
    return JsonResponse({"status": "ok"})
