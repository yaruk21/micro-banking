import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

from django.core.asgi import get_asgi_application

from .websocket_status import websocket_status_application

django_asgi_application = get_asgi_application()


async def application(scope, receive, send):
    """Handle application."""
    if scope["type"] == "websocket":
        await websocket_status_application(scope, receive, send)
        return

    await django_asgi_application(scope, receive, send)
