"""Shared helpers for HTML routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.auth import csrf_token_for


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a template with the current identity and CSRF token in the context."""
    env = request.app.state.templates
    html = env.get_template(template).render(
        identity=getattr(request.state, "identity", None),
        csrf_token=csrf_token_for(request),
        **context,
    )
    return HTMLResponse(html, status_code=status_code)


def wants_html(request: Request) -> bool:
    return request.method in ("GET", "HEAD", "POST") and not request.url.path.startswith(
        ("/api/", "/events")
    )
