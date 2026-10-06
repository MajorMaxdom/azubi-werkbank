"""Login, logout and invite pages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.common import render
from app.auth import (
    client_ip,
    end_session,
    password_problem,
    safe_next,
    start_session,
    verify_form,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    if getattr(request.state, "identity", None) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next=safe_next(next), error=None, username="")


@router.post("/login", dependencies=[Depends(verify_form)])
def login(
    request: Request,
    username: Annotated[str, Form(max_length=200)] = "",
    password: Annotated[str, Form(max_length=2048)] = "",
    next: Annotated[str, Form(max_length=2048)] = "/",
):
    login_service = request.app.state.login
    user = login_service.authenticate(username, password, client_ip(request))
    if user is None:
        return render(
            request,
            "login.html",
            status_code=401,
            next=safe_next(next),
            error="auth.login_failed",
            username=username[:63],
        )
    start_session(request, username.strip().lower())
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_form)])
def logout(request: Request) -> RedirectResponse:
    end_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str) -> HTMLResponse:
    accounts = request.app.state.accounts
    username = accounts.valid_invite(token)
    if username is None:
        return render(request, "message.html", status_code=404, message="auth.invite_invalid")
    user = accounts.directory.get(username)
    return render(request, "invite.html", user=user, username=username, error=None)


@router.post("/invite/{token}", dependencies=[Depends(verify_form)])
def invite_accept(
    request: Request,
    token: str,
    password: Annotated[str, Form(max_length=2048)] = "",
    password_repeat: Annotated[str, Form(max_length=2048)] = "",
):
    accounts = request.app.state.accounts
    username = accounts.valid_invite(token)
    if username is None:
        return render(request, "message.html", status_code=404, message="auth.invite_invalid")
    problem = password_problem(password, password_repeat)
    if problem:
        user = accounts.directory.get(username)
        return render(
            request, "invite.html", status_code=400, user=user, username=username, error=problem
        )
    if accounts.accept_invite(token, password) is None:
        return render(request, "message.html", status_code=404, message="auth.invite_invalid")
    start_session(request, username)
    return RedirectResponse("/", status_code=303)
