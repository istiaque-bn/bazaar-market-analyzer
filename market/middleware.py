"""Phase 9: request correlation IDs for structured logging."""
from __future__ import annotations

import uuid

from config.logging_utils import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Adopts an inbound X-Request-ID (so a request that already has one
    from an upstream proxy/load balancer keeps it end to end) or mints a
    short one, stores it in a ContextVar for the duration of the request
    so every log line emitted while handling it can be tagged, and
    echoes it back on the response so a client can correlate a support
    report with server-side logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "").strip()
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        request.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response
