"""Request ID middleware.

Generates a unique request_id for every HTTP request and stores it in contextvars.
Every subsequent logger call (in any layer) automatically carries this ID.
Usage: grep "rid=abc12345" logs/*/info_*.log -> follow the whole request chain.
"""

from src.log import generate_request_id, set_request_id


class RequestIdMiddleware:
    """Generate a unique request_id for every HTTP request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            rid = generate_request_id()
            set_request_id(rid)
        await self.app(scope, receive, send)
