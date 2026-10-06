"""Own account: change password, download own data. Also the data export (ZIP)
that Fachbetreuer can download for any user."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.api.common import render
from app.auth import (
    Identity,
    client_ip,
    get_current_user,
    password_problem,
    start_session,
    verify_form,
)
from app.progress import CorruptProgress
from app.renderer import ExportInfo, render_static

router = APIRouter()

CurrentUser = Annotated[Identity, Depends(get_current_user)]


# --------------------------------------------------------------------------- data export


def build_data_zip(request: Request, username: str, now: datetime) -> bytes:
    """ZIP with the user's users.yaml entry, raw progress files and one HTML
    snapshot per workbook. Never contains credentials."""
    state = request.app.state
    user = state.users.get(username)
    entry = {"username": username, **user.model_dump(mode="json")}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("user.json", json.dumps(entry, indent=2, ensure_ascii=False) + "\n")
        for workbook_id, path in state.progress.files_for(username):
            archive.writestr(f"progress/{workbook_id}.json", path.read_bytes())
            view = state.registry.view(workbook_id)
            if view is None:
                continue
            try:
                progress = state.progress.load(workbook_id, username)
            except CorruptProgress:
                continue
            html = render_static(
                view.catalog,
                trainer=False,
                workbooks_dir=state.config.paths.workbooks,
                t=state.translator,
                progress=progress,
                export=ExportInfo(name=user.name, username=username, exported_at=now),
                view=view,
            )
            archive.writestr(f"html/{workbook_id}.html", html)
    return buffer.getvalue()


def data_zip_response(request: Request, username: str) -> Response:
    now = datetime.now(UTC)
    filename = f"daten_{username}_{now:%Y-%m-%d}.zip"
    return Response(
        build_data_zip(request, username, now),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- pages


def account_page(
    request: Request, identity: Identity, status_code: int = 200, **context
) -> HTMLResponse:
    return render(
        request,
        "account.html",
        status_code=status_code,
        username=identity.username,
        user=identity.user,
        error=context.get("error"),
        saved=context.get("saved", False),
    )


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, identity: CurrentUser) -> HTMLResponse:
    return account_page(request, identity)


@router.post("/account", dependencies=[Depends(verify_form)], response_class=HTMLResponse)
def change_password(
    request: Request,
    identity: CurrentUser,
    current_password: Annotated[str, Form(max_length=2048)] = "",
    password: Annotated[str, Form(max_length=2048)] = "",
    password_repeat: Annotated[str, Form(max_length=2048)] = "",
) -> HTMLResponse:
    username = identity.username
    login_service = request.app.state.login
    if not login_service.check_current_password(username, current_password, client_ip(request)):
        return account_page(request, identity, status_code=400, error="account.wrong_password")
    problem = password_problem(password, password_repeat)
    if problem:
        return account_page(request, identity, status_code=400, error=problem)
    request.app.state.accounts.change_password(username, password)
    start_session(request, username)  # other sessions end, this one stays
    return account_page(request, identity, saved=True)


@router.get("/account/data")
def own_data(request: Request, identity: CurrentUser) -> Response:
    return data_zip_response(request, identity.username)
