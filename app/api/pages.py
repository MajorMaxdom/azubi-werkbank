"""HTML pages: start page, workbook view, per-workbook theme and catalog assets.
Every route here requires a logged-in user."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.api.common import render
from app.auth import Identity, get_current_user
from app.models.catalog import ID_PATTERN, is_safe_asset_path
from app.renderer import build_progress_view

router = APIRouter()

_ID_RE = re.compile(ID_PATTERN)
CurrentUser = Annotated[Identity, Depends(get_current_user)]


def asset_url(src: str) -> str:
    # `src` was validated to stay inside workbooks/assets/ and is served under /assets/.
    return "/" + src


def get_view(request: Request, workbook_id: str, identity: Identity):
    """The workbook view if it exists and the user may open it (else 404)."""
    if not _ID_RE.fullmatch(workbook_id) or not identity.user.may_open(workbook_id):
        raise HTTPException(status_code=404)
    view = request.app.state.registry.view(workbook_id)
    if view is None:
        raise HTTPException(status_code=404)
    return view


@router.get("/", response_class=HTMLResponse)
def index(request: Request, identity: CurrentUser) -> HTMLResponse:
    registry = request.app.state.registry
    store = request.app.state.progress
    entries = []
    for wid in registry.catalogs():
        view = registry.view(wid) if identity.user.may_open(wid) else None
        if view is None:
            continue
        pv = None
        if identity.user.role == "apprentice":
            pv = build_progress_view(view, store.load(wid, identity.username), editable=True)
        entries.append((view, pv))
    return render(request, "index.html", entries=entries)


@router.get("/workbooks/{workbook_id}", response_class=HTMLResponse)
def workbook(request: Request, workbook_id: str, identity: CurrentUser) -> HTMLResponse:
    view = get_view(request, workbook_id, identity)
    apprentice = identity.user.role == "apprentice"
    progress = (
        request.app.state.progress.load(workbook_id, identity.username) if apprentice else None
    )
    return render(
        request,
        "workbook.html",
        wb=view,
        is_trainer=identity.is_trainer,
        static=False,
        asset_url=asset_url,
        pv=build_progress_view(view, progress, editable=apprentice),
    )


@router.get("/workbooks/{workbook_id}/theme.css")
def theme(request: Request, workbook_id: str, identity: CurrentUser) -> Response:
    view = get_view(request, workbook_id, identity)
    etag = f'"{view.theme_hash}"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if etag in _etags(request.headers.get("if-none-match", "")):
        return Response(status_code=304, headers=headers)
    return Response(view.theme_css, media_type="text/css", headers=headers)


def _etags(header: str) -> set[str]:
    return {part.strip().removeprefix("W/") for part in header.split(",") if part.strip()}


@router.get("/assets/{path:path}")
def asset(request: Request, path: str, identity: CurrentUser) -> FileResponse:
    src = f"assets/{path}"
    if not is_safe_asset_path(src):
        raise HTTPException(status_code=404)
    workbooks = request.app.state.config.paths.workbooks
    root = (workbooks / "assets").resolve()
    target = (workbooks / src).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target)
