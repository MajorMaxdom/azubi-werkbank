"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import account, admin, auth, editor, events, pages, progress
from app.api.common import render, wants_html
from app.auth import (
    AccountService,
    AuthMiddleware,
    CredentialStore,
    CsrfFailed,
    Forbidden,
    IpRateLimiter,
    LoginRequired,
    LoginService,
    Sessions,
    UserDirectory,
    load_or_create_secret,
)
from app.config import Config, load_config
from app.i18n import Translator
from app.loader import Registry
from app.progress import CorruptProgress, ProgressStore
from app.renderer import STATIC_DIR, create_environment, set_display_timezone

log = logging.getLogger(__name__)

ERROR_MESSAGES = {
    400: "errors.page_bad_request",
    403: "errors.page_forbidden",
    404: "errors.page_not_found",
}


def create_app(config: Config | None = None, *, watch: bool = True) -> FastAPI:
    config = config or load_config()
    set_display_timezone(config.timezone)
    translator = Translator.from_file(config.paths.locales / "de.yaml")
    registry = Registry(config.paths.workbooks, translator)
    broadcaster = events.Broadcaster()
    registry.on_change(broadcaster.publish)

    users = UserDirectory(config.paths.users)
    credentials = CredentialStore(config.paths.data / "credentials.json")
    accounts = AccountService(config, users, credentials)
    sessions = Sessions(config, load_or_create_secret(config.paths.data))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broadcaster.bind(asyncio.get_running_loop())
        registry.load_all()
        users.load()
        stop = asyncio.Event()
        tasks = []
        if watch:
            tasks = [
                asyncio.create_task(registry.watch(stop)),
                asyncio.create_task(users.watch(stop)),
            ]
        try:
            yield
        finally:
            stop.set()
            broadcaster.close()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.translator = translator
    app.state.registry = registry
    app.state.broadcaster = broadcaster
    app.state.templates = create_environment(translator)
    app.state.users = users
    app.state.credentials = credentials
    app.state.accounts = accounts
    app.state.sessions = sessions
    app.state.login = LoginService(config, accounts, IpRateLimiter())
    app.state.progress = ProgressStore(config.paths.progress)

    app.add_middleware(AuthMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(pages.router)
    app.include_router(admin.router)
    app.include_router(events.router)
    app.include_router(progress.router)
    app.include_router(editor.router)
    app.include_router(editor.api)
    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        if request.method == "GET" and wants_html(request):
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
        return Response(status_code=401)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        return _error_page(request, 403)

    @app.exception_handler(CsrfFailed)
    async def csrf_failed(request: Request, exc: CsrfFailed) -> Response:
        log.warning("auth.csrf_failed path=%s", request.url.path)
        if wants_html(request):
            return render(request, "message.html", status_code=403, message="errors.csrf")
        return Response(status_code=403)

    @app.exception_handler(CorruptProgress)
    async def corrupt_progress(request: Request, exc: CorruptProgress) -> Response:
        if wants_html(request):
            return render(
                request, "message.html", status_code=500, message="errors.progress_corrupt"
            )
        return Response(status_code=500)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        return _error_page(request, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        return _error_page(request, 400)


def _error_page(request: Request, status_code: int) -> Response:
    if not wants_html(request):
        return Response(status_code=status_code)
    message = ERROR_MESSAGES.get(status_code, "errors.page_generic")
    return render(request, "message.html", status_code=status_code, message=message)
