"""JSON API for progress.

Apprentice endpoints may only touch ``header``, ``answers`` and ``done`` of
their OWN progress (the user comes from the session, never from the URL or
body). Fachbetreuer endpoints may only touch ``review`` and ``signoff``; the
username in their URLs names the apprentice being reviewed, while the acting
Fachbetreuer again comes from the session."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.auth import Identity, get_current_user, is_valid_username, verify_json_api
from app.models.catalog import Catalog, HeaderField
from app.models.progress import Progress, Review, Signoff, TaskProgress
from app.progress import (
    MAX_ANSWER_CHARS,
    MAX_REQUEST_BYTES,
    InvalidInput,
    all_task_ids,
    find_task,
    utcnow,
    validate_answers,
    validate_header,
)
from app.renderer import task_hash

router = APIRouter(prefix="/api/progress", dependencies=[Depends(verify_json_api)])

CurrentUser = Annotated[Identity, Depends(get_current_user)]


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] | None = None
    done: StrictBool | None = None


async def read_json(request: Request) -> Any:
    """Request body as JSON, at most ``MAX_REQUEST_BYTES`` (else 413)."""
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > MAX_REQUEST_BYTES):
        raise HTTPException(status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413)
    try:
        return json.loads(body or b"null")
    except ValueError as exc:
        raise HTTPException(status_code=422) from exc


def apprentice_catalog(request: Request, workbook_id: str, identity: Identity) -> Catalog:
    if identity.user.role != "apprentice":
        raise HTTPException(status_code=403)  # Fachbetreuer only write review/sign-off
    if not identity.user.may_open(workbook_id):
        raise HTTPException(status_code=404)
    catalog = request.app.state.registry.get(workbook_id)
    if catalog is None:
        raise HTTPException(status_code=404)
    return catalog


def summary(catalog: Catalog, progress: Progress) -> dict[str, int]:
    ids = all_task_ids(catalog)
    return {"done": progress.done_count(ids), "total": len(ids)}


def unprocessable(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=422)


@router.patch("/{workbook_id}/header")
async def patch_header(request: Request, workbook_id: str, identity: CurrentUser):
    catalog = apprentice_catalog(request, workbook_id, identity)
    data = await read_json(request)
    if not isinstance(data, dict):
        return unprocessable("body must be an object of header field values")
    try:
        values = validate_header(catalog.workbook.header_fields, data)
    except InvalidInput as exc:
        return unprocessable(str(exc))

    def change(progress: Progress) -> None:
        progress.header.update(values)

    progress = request.app.state.progress.update(catalog, identity.username, change)
    return {"updated_at": progress.updated_at, "progress": summary(catalog, progress)}


@router.patch("/{workbook_id}/tasks/{task_id}")
async def patch_task(request: Request, workbook_id: str, task_id: str, identity: CurrentUser):
    catalog = apprentice_catalog(request, workbook_id, identity)
    task = find_task(catalog, task_id)
    if task is None:
        return unprocessable(f"unknown task id '{task_id}'")
    try:
        patch = TaskPatch.model_validate(await read_json(request))
    except ValidationError:
        return unprocessable("body must be {answers?: {...}, done?: bool}")
    try:
        answers = validate_answers(task, patch.answers or {})
    except InvalidInput as exc:
        return unprocessable(str(exc))
    current_hash = task_hash(task)

    def change(progress: Progress) -> None:
        tp = progress.tasks.get(task_id) or TaskProgress()
        now = utcnow()
        if answers:
            tp.answers.update(answers)
        if patch.done is not None and patch.done != tp.done:
            tp.done = patch.done
            tp.done_at = now if patch.done else None
        if answers or patch.done is not None:
            tp.task_hash = current_hash
            tp.updated_at = now
        progress.tasks[task_id] = tp

    progress = request.app.state.progress.update(catalog, identity.username, change)
    tp = progress.tasks[task_id]
    return {
        "updated_at": progress.updated_at,
        "task": {"done": tp.done, "done_at": tp.done_at, "changed": tp.task_hash != current_hash},
        "progress": summary(catalog, progress),
    }


# --------------------------------------------------------------------------- Fachbetreuer


class ReviewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "redo"] | None = None
    comment: Annotated[str, Field(max_length=MAX_ANSWER_CHARS)] = ""


class SignoffPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: Annotated[str, Field(max_length=MAX_ANSWER_CHARS)] = ""
    date: str = ""


def reviewed_catalog(
    request: Request, workbook_id: str, username: str, identity: Identity
) -> Catalog:
    """Catalog for reviewing ``username``'s workbook; only Fachbetreuer (403 otherwise)."""
    if not identity.is_trainer:
        raise HTTPException(status_code=403)
    user = request.app.state.users.get(username) if is_valid_username(username) else None
    if user is None or user.role != "apprentice" or not user.may_open(workbook_id):
        raise HTTPException(status_code=404)
    catalog = request.app.state.registry.get(workbook_id)
    if catalog is None:
        raise HTTPException(status_code=404)
    return catalog


@router.patch("/{workbook_id}/users/{username}/tasks/{task_id}/review")
async def patch_review(
    request: Request, workbook_id: str, username: str, task_id: str, identity: CurrentUser
):
    catalog = reviewed_catalog(request, workbook_id, username, identity)
    if find_task(catalog, task_id) is None:
        return unprocessable(f"unknown task id '{task_id}'")
    try:
        data = ReviewPatch.model_validate(await read_json(request))
    except ValidationError:
        return unprocessable("body must be {status: 'ok'|'redo'|null, comment: str}")

    def change(progress: Progress) -> None:
        tp = progress.tasks.get(task_id) or TaskProgress()
        if data.status is None and not data.comment:
            tp.review = None
        else:
            tp.review = Review(
                status=data.status,
                comment=data.comment,
                reviewed_by=identity.username,
                reviewed_at=utcnow(),
            )
        progress.tasks[task_id] = tp

    progress = request.app.state.progress.update(catalog, username, change)
    review = progress.tasks[task_id].review
    return {"updated_at": progress.updated_at, "review": review}


@router.patch("/{workbook_id}/users/{username}/signoff")
async def patch_signoff(request: Request, workbook_id: str, username: str, identity: CurrentUser):
    catalog = reviewed_catalog(request, workbook_id, username, identity)
    if not catalog.workbook.signoff.enabled:
        return unprocessable("sign-off is disabled for this workbook")
    try:
        data = SignoffPatch.model_validate(await read_json(request))
        signed_on = validate_header(
            [HeaderField(id="date", label="date", type="date")], {"date": data.date}
        )["date"]
    except (ValidationError, InvalidInput):
        return unprocessable("body must be {comment: str, date: 'YYYY-MM-DD' or ''}")

    def change(progress: Progress) -> None:
        progress.signoff = Signoff(
            comment=data.comment,
            date=signed_on or None,
            by=identity.username if (signed_on or data.comment) else None,
        )

    progress = request.app.state.progress.update(catalog, username, change)
    return {"updated_at": progress.updated_at, "signoff": progress.signoff}
