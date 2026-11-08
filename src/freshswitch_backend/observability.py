"""Structured operational events without request bodies, credentials or provider errors."""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

request_id = ContextVar("freshswitch_request_id", default=None)
request_tags: ContextVar[dict | None] = ContextVar("freshswitch_request_tags", default=None)
logger = logging.getLogger("freshswitch.audit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.propagate = False
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FIELDS = {
    "method",
    "route",
    "status",
    "elapsed_ms",
    "event_type",
    "model",
    "source",
    "fallback_reason",
    "error_type",
    "budget_seconds",
    "shipper_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "org_type",
    "sandbox",
    "lot_count",
}
TAG_FIELDS = frozenset({"org_type", "sandbox"})


def record_event(event: str, **fields) -> None:
    payload = {"event": event, "request_id": request_id.get()}
    payload.update({key: value for key, value in fields.items() if key in FIELDS})
    logger.info(json.dumps(payload, ensure_ascii=False))


def tag_request(**fields) -> None:
    tags = request_tags.get()
    if tags is not None:
        tags.update({key: value for key, value in fields.items() if key in TAG_FIELDS})


class RequestAuditMiddleware:
    """Pure ASGI middleware preserves streaming and propagates IDs into worker threads."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        supplied = next(
            (
                v.decode("latin-1")
                for k, v in scope.get("headers", [])
                if k.lower() == b"x-request-id"
            ),
            "",
        )
        current_id = supplied if SAFE_ID.fullmatch(supplied) else uuid.uuid4().hex
        token = request_id.set(current_id)
        tags: dict = {}
        tags_token = request_tags.set(tags)
        started = time.perf_counter()
        status = 500

        async def respond(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"x-request-id"
                    ]
                    + [(b"x-request-id", current_id.encode("ascii"))],
                }
            await send(message)

        try:
            await self.app(scope, receive, respond)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            record_event(
                "http.request",
                method=scope["method"],
                route=route,
                status=status,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                org_type=tags.get("org_type"),
                sandbox=tags.get("sandbox"),
            )
            request_tags.reset(tags_token)
            request_id.reset(token)
