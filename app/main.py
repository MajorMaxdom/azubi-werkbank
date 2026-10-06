"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import admin, events, pages
from app.config import Config, load_config
from app.i18n import Translator
from app.loader import Registry
from app.renderer import STATIC_DIR, create_environment

log = logging.getLogger(__name__)


class OptionalStaticFiles(StaticFiles):
    """StaticFiles that answers 404 (instead of crashing) while its directory is missing."""

    async def check_config(self) -> None:
        if self.directory is not None and Path(self.directory).is_dir():
            await super().check_config()

    def lookup_path(self, path: str):
        if self.directory is not None and not Path(self.directory).is_dir():
            return "", None
        return super().lookup_path(path)


def create_app(config: Config | None = None, *, watch: bool = True) -> FastAPI:
    config = config or load_config()
    translator = Translator.from_file(config.paths.locales / "de.yaml")
    registry = Registry(config.paths.workbooks, translator)
    broadcaster = events.Broadcaster()
    registry.on_change(broadcaster.publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broadcaster.bind(asyncio.get_running_loop())
        registry.load_all()
        stop = asyncio.Event()
        task = asyncio.create_task(registry.watch(stop)) if watch else None
        try:
            yield
        finally:
            stop.set()
            broadcaster.close()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.translator = translator
    app.state.registry = registry
    app.state.broadcaster = broadcaster
    app.state.templates = create_environment(translator)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount(
        "/assets",
        OptionalStaticFiles(directory=config.paths.workbooks / "assets", check_dir=False),
        name="assets",
    )
    app.include_router(pages.router)
    app.include_router(admin.router)
    app.include_router(events.router)
    return app
