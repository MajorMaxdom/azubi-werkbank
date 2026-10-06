"""HTML pages: start page, workbook view and the per-workbook theme stylesheet."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.models.catalog import ID_PATTERN

router = APIRouter()

_ID_RE = re.compile(ID_PATTERN)


def asset_url(src: str) -> str:
    # `src` was validated to stay inside workbooks/assets/ and is served under /assets/.
    return "/" + src


def get_view(request: Request, workbook_id: str):
    if not _ID_RE.fullmatch(workbook_id):
        raise HTTPException(status_code=404)
    view = request.app.state.registry.view(workbook_id)
    if view is None:
        raise HTTPException(status_code=404)
    return view


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    registry = request.app.state.registry
    views = [registry.view(wid) for wid in registry.catalogs()]
    html = request.app.state.templates.get_template("index.html").render(
        workbooks=[v for v in views if v is not None]
    )
    return HTMLResponse(html)


@router.get("/workbooks/{workbook_id}", response_class=HTMLResponse)
def workbook(request: Request, workbook_id: str) -> HTMLResponse:
    view = get_view(request, workbook_id)
    html = request.app.state.templates.get_template("workbook.html").render(
        wb=view,
        is_trainer=False,  # roles arrive with authentication in 0.3.0
        static=False,
        asset_url=asset_url,
    )
    return HTMLResponse(html)


@router.get("/workbooks/{workbook_id}/theme.css")
def theme(request: Request, workbook_id: str) -> Response:
    view = get_view(request, workbook_id)
    etag = f'"{view.theme_hash}"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if etag in _etags(request.headers.get("if-none-match", "")):
        return Response(status_code=304, headers=headers)
    return Response(view.theme_css, media_type="text/css", headers=headers)


def _etags(header: str) -> set[str]:
    return {part.strip().removeprefix("W/") for part in header.split(",") if part.strip()}
