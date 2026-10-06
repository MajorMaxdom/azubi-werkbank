"""HTML pages: start page, workbook view, per-workbook theme and catalog assets.
Every route here requires a logged-in user."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.api.common import render
from app.auth import Forbidden, Identity, get_current_user
from app.models.catalog import ID_PATTERN, is_safe_asset_path
from app.models.users import User
from app.progress import (
    TASK_STATES,
    needs_check,
    orphaned_answers,
    responsible_tasks,
    task_state,
)
from app.renderer import (
    ExportInfo,
    ProgressView,
    ReviewContext,
    WorkbookView,
    build_progress_view,
    render_static,
)

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


@dataclass
class Assignment:
    """One apprentice workbook a Fachbetreuer is responsible for (fully or in part)."""

    username: str
    user: User
    view: WorkbookView
    pv: ProgressView
    tasks: list[str]  # responsible task ids
    partial: bool
    open_checks: int
    numbers: list[str]


def assignments_for(request: Request, trainer: str) -> list[Assignment]:
    registry = request.app.state.registry
    store = request.app.state.progress
    result = []
    for username, user in sorted(request.app.state.users.all().items()):
        if user.role != "apprentice" or not user.active:
            continue
        for wid in registry.catalogs():
            if not user.may_open(wid):
                continue
            view = registry.view(wid)
            if view is None:
                continue
            task_ids = list(view.tasks_by_id)
            mine = responsible_tasks(user, wid, trainer, task_ids)
            if not mine:
                continue
            progress = store.load(wid, username)
            pv = build_progress_view(view, progress, editable=False)
            result.append(
                Assignment(
                    username=username,
                    user=user,
                    view=view,
                    pv=pv,
                    tasks=mine,
                    partial=len(mine) < len(task_ids),
                    open_checks=sum(needs_check(progress.tasks.get(tid)) for tid in mine),
                    numbers=[view.tasks_by_id[tid].number for tid in mine],
                )
            )
    return result


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
    assignments = assignments_for(request, identity.username) if identity.is_trainer else []
    return render(request, "index.html", entries=entries, assignments=assignments)


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


@router.get("/workbooks/{workbook_id}/users/{username}", response_class=HTMLResponse)
def review(request: Request, workbook_id: str, username: str, identity: CurrentUser):
    if not identity.is_trainer:
        raise Forbidden
    view = get_view(request, workbook_id, identity)
    user = request.app.state.users.get(username) if _ID_RE.fullmatch(username) else None
    if user is None or user.role != "apprentice" or not user.may_open(workbook_id):
        raise HTTPException(status_code=404)
    progress = request.app.state.progress.load(workbook_id, username)
    users = request.app.state.users.all()
    names = {name: u.name for name, u in users.items()}
    supervisors = {}
    for tid in view.tasks_by_id:
        sup = user.supervisor_for(workbook_id, tid)
        if sup:
            supervisors[tid] = names.get(sup, sup)
    context = ReviewContext(
        username=username,
        user=user,
        supervisors=supervisors,
        names=names,
        orphans=orphaned_answers(view.catalog, progress),
    )
    return render(
        request,
        "workbook.html",
        wb=view,
        is_trainer=True,
        static=False,
        asset_url=asset_url,
        pv=build_progress_view(view, progress, editable=False),
        review=context,
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


@router.get("/workbooks/{workbook_id}/export")
def export(request: Request, workbook_id: str, identity: CurrentUser, user: str | None = None):
    """Self-contained, print-friendly HTML snapshot (download)."""
    view = get_view(request, workbook_id, identity)
    users = request.app.state.users
    review = None
    if user is not None and user != identity.username:
        if not identity.is_trainer:
            raise Forbidden
        target = users.get(user) if _ID_RE.fullmatch(user) else None
        if target is None or target.role != "apprentice" or not target.may_open(workbook_id):
            raise HTTPException(status_code=404)
        username, display = user, target.name
    elif identity.is_trainer:
        username, display = None, ""
    else:
        username, display = identity.username, identity.user.name

    progress = request.app.state.progress.load(workbook_id, username) if username else None
    if identity.is_trainer and username:
        names = {name: u.name for name, u in users.all().items()}
        target = users.get(username)
        supervisors = {}
        for tid in view.tasks_by_id:
            sup = target.supervisor_for(workbook_id, tid)
            if sup:
                supervisors[tid] = names.get(sup, sup)
        review = ReviewContext(
            username=username,
            user=target,
            supervisors=supervisors,
            names=names,
            orphans=orphaned_answers(view.catalog, progress),
        )
    now = datetime.now(UTC)
    html = render_static(
        view.catalog,
        trainer=identity.is_trainer,
        workbooks_dir=request.app.state.config.paths.workbooks,
        t=request.app.state.translator,
        progress=progress,
        export=ExportInfo(name=display, username=username or "", exported_at=now),
        review=review,
        view=view,
    )
    filename = f"arbeitsheft_{workbook_id}_{username or 'leer'}_{now:%Y-%m-%d}.html"
    return HTMLResponse(
        html,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- my tasks


@dataclass
class TaskRow:
    number: str
    title: str
    task_id: str
    module: str
    state: str
    state_label: str
    done_at: object
    supervisor: str
    comment: str
    link: str


@dataclass
class TaskGroup:
    title: str  # workbook title (apprentice) or "<apprentice> · <workbook>" (trainer)
    link: str
    rows: list[TaskRow]
    done: int
    total: int


STATE_KEYS = {state: f"my_tasks.state_{state}" for state in TASK_STATES}


def _rows(request, view, progress, user, username_for_link, task_ids=None) -> list[TaskRow]:
    t = request.app.state.translator
    names = {n: u.name for n, u in request.app.state.users.all().items()}
    rows = []
    wid = view.meta.id
    for day in view.days:
        for module in day.modules:
            for tv in module.tasks:
                if task_ids is not None and tv.task.id not in task_ids:
                    continue
                tp = progress.tasks.get(tv.task.id)
                state = task_state(tp)
                sup = user.supervisor_for(wid, tv.task.id)
                base = (
                    f"/workbooks/{wid}/users/{username_for_link}"
                    if username_for_link
                    else f"/workbooks/{wid}"
                )
                rows.append(
                    TaskRow(
                        number=tv.number,
                        title=tv.task.title,
                        task_id=tv.task.id,
                        module=module.module.code,
                        state=state,
                        state_label=t(STATE_KEYS[state]),
                        done_at=tp.done_at if tp and tp.done else None,
                        supervisor=names.get(sup, sup) if sup else "",
                        comment=tp.review.comment if tp and tp.review else "",
                        link=f"{base}#task-{tv.task.id}",
                    )
                )
    return rows


@router.get("/my-tasks", response_class=HTMLResponse)
def my_tasks(request: Request, identity: CurrentUser, filter: str = "all") -> HTMLResponse:
    registry = request.app.state.registry
    store = request.app.state.progress
    only_open = filter == "open"
    groups: list[TaskGroup] = []
    if identity.user.role == "apprentice":
        for wid in registry.catalogs():
            view = registry.view(wid) if identity.user.may_open(wid) else None
            if view is None:
                continue
            progress = store.load(wid, identity.username)
            rows = _rows(request, view, progress, identity.user, None)
            if only_open:
                rows = [r for r in rows if r.state in ("open", "redo")]
            groups.append(
                TaskGroup(
                    title=view.meta.title,
                    link=f"/workbooks/{wid}",
                    rows=rows,
                    done=progress.done_count(set(view.tasks_by_id)),
                    total=view.task_count,
                )
            )
    else:
        for a in assignments_for(request, identity.username):
            progress = store.load(a.view.meta.id, a.username)
            rows = _rows(request, a.view, progress, a.user, a.username, set(a.tasks))
            if only_open:
                rows = [r for r in rows if r.state in ("waiting", "recheck")]
            groups.append(
                TaskGroup(
                    title=f"{a.user.name} · {a.view.meta.title}",
                    link=f"/workbooks/{a.view.meta.id}/users/{a.username}",
                    rows=rows,
                    done=a.pv.done,
                    total=a.pv.total,
                )
            )
    return render(request, "my_tasks.html", groups=groups, only_open=only_open)
