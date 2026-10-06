"""Form editor for workbooks (Fachbetreuer only)."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.api.admin import localize_error
from app.api.common import render
from app.api.progress import read_json
from app.auth import Identity, require_trainer, verify_form, verify_json_api_any
from app.editor import (
    MAX_ASSET_BYTES,
    AssetTooLarge,
    EditorConflict,
    asset_directory,
    asset_usage,
    clean,
    is_asset_name,
    list_assets,
    load_for_editor,
    locked_ids,
    missing_locked,
    new_workbook_data,
    save,
    store_asset,
    validate,
)
from app.loader import CatalogError
from app.models.catalog import ID_PATTERN, Catalog, Level, Task
from app.renderer import build_progress_view, build_view

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_trainer)])
Trainer = Annotated[Identity, Depends(require_trainer)]

_ID_RE = re.compile(ID_PATTERN)
# Path segments of the editor API that must never be workbook ids.
RESERVED_IDS = {"preview", "strings"}
NEW_FILE_HEADER = "# Workbook catalog — edited with the form editor; see docs/AUTHORING.md.\n"


def _entry(request: Request, workbook_id: str):
    if not _ID_RE.fullmatch(workbook_id):
        raise HTTPException(status_code=404)
    entry = request.app.state.registry.entry_for(workbook_id)
    if entry is None or not entry.path.exists():
        raise HTTPException(status_code=404)
    return entry


def _errors(request: Request, errors: list[CatalogError]) -> list[dict[str, str]]:
    t = request.app.state.translator
    return [
        {"path": ".".join(str(p) for p in e.loc), "message": localize_error(e, t)} for e in errors
    ]


# --------------------------------------------------------------------------- pages


@router.get("/admin/editor", response_class=HTMLResponse)
def editor_index(request: Request, error: str | None = None) -> HTMLResponse:
    registry = request.app.state.registry
    return render(
        request,
        "editor_index.html",
        entries=registry.entries(),
        catalogs=registry.catalogs(),
        error=error,
        form={"title": "", "id": "", "source": ""},
    )


@router.post("/admin/editor", dependencies=[Depends(verify_form)], response_class=HTMLResponse)
def create_workbook(
    request: Request,
    title: Annotated[str, Form(max_length=200)] = "",
    workbook_id: Annotated[str, Form(alias="id", max_length=63)] = "",
    source: Annotated[str, Form(max_length=63)] = "",
):
    registry = request.app.state.registry
    title = title.strip()
    workbook_id = workbook_id.strip().lower()
    form = {"title": title, "id": workbook_id, "source": source}

    def fail(key: str) -> HTMLResponse:
        return render(
            request,
            "editor_index.html",
            status_code=400,
            entries=registry.entries(),
            catalogs=registry.catalogs(),
            error=key,
            form=form,
        )

    if not title:
        return fail("editor.error_title")
    if not _ID_RE.fullmatch(workbook_id) or workbook_id in RESERVED_IDS:
        return fail("editor.error_id")
    path = registry.directory / f"{workbook_id}.yaml"
    if registry.get(workbook_id) is not None or path.exists():
        return fail("editor.error_exists")
    source_data = None
    if source:
        entry = registry.entry_for(source) if _ID_RE.fullmatch(source) else None
        if entry is None:
            return fail("editor.error_source")
        source_data, _ = load_for_editor(entry.path)
    data = new_workbook_data(workbook_id, title, source_data)
    catalog, _errors = validate(data)
    if catalog is None:
        return fail("editor.error_source")
    save(path, data, None, header=NEW_FILE_HEADER)
    registry.apply_changes({path})
    return RedirectResponse(f"/admin/editor/{workbook_id}", status_code=303)


@router.get("/admin/editor/{workbook_id}", response_class=HTMLResponse)
def editor_page(request: Request, workbook_id: str) -> HTMLResponse:
    entry = _entry(request, workbook_id)
    return render(request, "editor.html", workbook_id=workbook_id, entry=entry)


# --------------------------------------------------------------------------- JSON API


api = APIRouter(
    prefix="/api/editor", dependencies=[Depends(require_trainer), Depends(verify_json_api_any)]
)


@api.get("/strings")
def strings(request: Request) -> dict[str, Any]:
    translator = request.app.state.translator
    return {**translator.lookup("editor_ui"), **translator.lookup("editor_assets")}


@api.get("/{workbook_id}")
def load(request: Request, workbook_id: str) -> dict[str, Any]:
    entry = _entry(request, workbook_id)
    data, base_hash = load_for_editor(entry.path)
    locked_map = locked_ids(request.app.state.config.paths.progress, workbook_id)
    return {
        "data": data,
        "base_hash": base_hash,
        "file": entry.path.name,
        "locked": {tid: sorted(aids) for tid, aids in locked_map.items()},
    }


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: dict[str, Any]
    levels: dict[str, Any] = {}


@api.post("/preview")
async def preview(request: Request):
    try:
        body = PreviewRequest.model_validate(await read_json(request))
    except ValidationError:
        return JSONResponse({"error": "invalid request"}, status_code=422)
    task_data = clean(body.task, top=False)
    levels = {}
    for key, value in clean(body.levels, top=False).items():
        try:
            levels[str(key)] = Level.model_validate(value)
        except ValidationError:
            continue
    try:
        task = Task.model_validate(task_data)
    except ValidationError as exc:
        errors = [
            CatalogError("preview", tuple(e["loc"]), e["type"], e["msg"], None,
                         {k: str(v) for k, v in (e.get("ctx") or {}).items()})
            for e in exc.errors(include_url=False)
        ]  # fmt: skip
        return JSONResponse({"errors": _errors(request, errors)}, status_code=422)
    catalog = Catalog.model_validate(
        {
            "schema_version": 1,
            "workbook": {"id": "preview", "version": "1.0.0", "title": "Vorschau"},
            "levels": {k: v.model_dump() for k, v in levels.items()}
            or {task.level: {"label": task.level, "color": "grey"}},
            "days": [{"id": "d", "title": "d", "modules": [
                {"code": "M", "title": "M", "tasks": [task.model_dump(exclude_none=True)]}]}],
        }
    )  # fmt: skip
    view = build_view(catalog, request.app.state.translator)
    tv = view.tasks_by_id[task.id]
    if task.number is None:
        tv.number = "–"
    html = request.app.state.templates.get_template("partials/task.html").render(
        tv=tv,
        wb=view,
        is_trainer=True,
        static=False,
        export=None,
        review=None,
        locked=False,
        pv=build_progress_view(view, None, editable=False),
        asset_url=lambda src: "/" + src,
    )
    return {"html": html}


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    base_hash: str
    confirm_delete: dict[str, list[str]] = {}


@api.post("/{workbook_id}")
async def store(request: Request, workbook_id: str):
    entry = _entry(request, workbook_id)
    try:
        body = SaveRequest.model_validate(await read_json(request))
    except ValidationError:
        return JSONResponse({"error": "invalid request"}, status_code=422)
    data = clean(body.data)
    catalog, errors = validate(data, entry.path.name)
    if catalog is not None and catalog.workbook.id != workbook_id:
        errors = [CatalogError(entry.path.name, ("workbook", "id"), "id_changed", "id changed")]
        catalog = None
    if catalog is None:
        return JSONResponse({"errors": _errors(request, errors)}, status_code=422)

    locked_map = locked_ids(request.app.state.config.paths.progress, workbook_id)
    missing = missing_locked(catalog, locked_map)
    unconfirmed = {
        tid: aids
        for tid, aids in missing.items()
        if tid not in body.confirm_delete or set(aids) - set(body.confirm_delete[tid])
    }
    if unconfirmed:
        return JSONResponse({"confirm": unconfirmed}, status_code=409)
    try:
        new_hash = save(entry.path, data, body.base_hash)
    except EditorConflict:
        return JSONResponse({"conflict": True}, status_code=409)
    request.app.state.registry.apply_changes({entry.path})
    return {"base_hash": new_hash, "version": catalog.workbook.version}


# --------------------------------------------------------------------------- image assets

UPLOAD_OVERHEAD = 64 * 1024  # multipart boundaries and part headers
UPLOAD_CHUNK = 64 * 1024


def _asset_src(workbook_id: str, name: str) -> str:
    return f"assets/{workbook_id}/{name}"


@api.get("/{workbook_id}/assets")
def assets(request: Request, workbook_id: str) -> list[dict[str, Any]]:
    _entry(request, workbook_id)
    directory = asset_directory(request.app.state.config.paths.workbooks, workbook_id)
    return [{"src": _asset_src(workbook_id, a["name"]), **a} for a in list_assets(directory)]


@api.post("/{workbook_id}/assets")
async def upload_asset(request: Request, workbook_id: str):
    """Store one uploaded image (multipart field ``file``) for this workbook."""
    _entry(request, workbook_id)
    limit = MAX_ASSET_BYTES + UPLOAD_OVERHEAD
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > limit):
        return JSONResponse({"error": "too_large"}, status_code=413)

    received = 0
    receive = request.receive

    async def limited_receive():
        nonlocal received
        message = await receive()
        received += len(message.get("body", b""))
        if received > limit:
            raise AssetTooLarge
        return message

    limited = Request(request.scope, limited_receive)
    try:
        form = await limited.form(max_files=1, max_fields=1)
    except AssetTooLarge:
        return JSONResponse({"error": "too_large"}, status_code=413)
    except HTTPException:
        return JSONResponse({"error": "invalid request"}, status_code=422)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return JSONResponse({"error": "invalid request"}, status_code=422)

        def chunks():
            while chunk := upload.file.read(UPLOAD_CHUNK):
                yield chunk

        directory = asset_directory(request.app.state.config.paths.workbooks, workbook_id)
        try:
            name = await run_in_threadpool(store_asset, directory, upload.filename, chunks())
        except AssetTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "unsupported_type"}, status_code=415)
    finally:
        await form.close()
    log.info("editor.asset_uploaded workbook=%s name=%s", workbook_id, name)
    return {"src": _asset_src(workbook_id, name), "name": name}


def _delete_unused_asset(request: Request, workbook_id: str, name: str) -> str | None:
    """Delete one image if nothing uses it. Returns its src, or None if it is in use."""
    directory = asset_directory(request.app.state.config.paths.workbooks, workbook_id)
    if not is_asset_name(name) or name not in {a["name"] for a in list_assets(directory)}:
        raise HTTPException(status_code=404)
    src = _asset_src(workbook_id, name)
    if src in _asset_usage(request):
        return None
    (directory / name).unlink(missing_ok=True)
    log.info("editor.asset_deleted workbook=%s name=%s", workbook_id, name)
    return src


def _asset_usage(request: Request) -> dict[str, list[str]]:
    registry = request.app.state.registry
    views = [registry.view(wid) for wid in registry.catalogs()]
    return asset_usage(v for v in views if v is not None)


@api.post("/{workbook_id}/assets/{name}/delete")
def delete_asset(request: Request, workbook_id: str, name: str):
    """Remove an image that nothing in any loaded workbook uses."""
    _entry(request, workbook_id)
    src = _delete_unused_asset(request, workbook_id, name)
    if src is None:
        return JSONResponse({"error": "in_use"}, status_code=409)
    return {"deleted": src}


# --------------------------------------------------------------------------- image housekeeping


@router.get("/admin/editor/{workbook_id}/assets", response_class=HTMLResponse)
def assets_page(request: Request, workbook_id: str, done: str = "") -> HTMLResponse:
    entry = _entry(request, workbook_id)
    directory = asset_directory(request.app.state.config.paths.workbooks, workbook_id)
    usage = _asset_usage(request)
    images = []
    for asset in list_assets(directory):
        src = _asset_src(workbook_id, asset["name"])
        images.append({**asset, "src": src, "used_in": usage.get(src, [])})
    unused = [i for i in images if not i["used_in"]]
    return render(
        request,
        "editor_assets.html",
        workbook_id=workbook_id,
        entry=entry,
        images=images,
        unused=unused,
        unused_bytes=sum(i["size"] for i in unused),
        done=done if done in ("deleted", "cleaned", "in_use") else "",
    )


@router.post(
    "/admin/editor/{workbook_id}/assets/{name}/delete", dependencies=[Depends(verify_form)]
)
def delete_asset_form(request: Request, workbook_id: str, name: str) -> RedirectResponse:
    _entry(request, workbook_id)
    src = _delete_unused_asset(request, workbook_id, name)
    result = "deleted" if src else "in_use"
    return RedirectResponse(f"/admin/editor/{workbook_id}/assets?done={result}", status_code=303)


@router.post("/admin/editor/{workbook_id}/assets/cleanup", dependencies=[Depends(verify_form)])
def cleanup_assets(request: Request, workbook_id: str) -> RedirectResponse:
    _entry(request, workbook_id)
    directory = asset_directory(request.app.state.config.paths.workbooks, workbook_id)
    for asset in list_assets(directory):
        _delete_unused_asset(request, workbook_id, asset["name"])
    return RedirectResponse(f"/admin/editor/{workbook_id}/assets?done=cleaned", status_code=303)
