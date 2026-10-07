"""Security headers on every response.

Behind Caddy the site config sets the same headers (deploy/Caddyfile); when the
app serves HTTPS itself (``tls_cert``/``tls_key``), these are the only ones.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

BASE_HEADERS = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    # Not "no-referrer": with it, browsers send "Origin: null" on form POSTs.
    ("referrer-policy", "same-origin"),
    ("content-security-policy", CONTENT_SECURITY_POLICY),
)
HSTS = ("strict-transport-security", "max-age=31536000")


class SecurityHeadersMiddleware:
    """Adds the headers above unless a response already sets them."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        headers = [*BASE_HEADERS, HSTS] if hsts else list(BASE_HEADERS)
        self.headers = [(name.encode(), value.encode()) for name, value in headers]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in self.headers if n not in existing]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)
