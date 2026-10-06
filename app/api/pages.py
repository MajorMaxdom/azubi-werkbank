"""HTML pages: start page, workbook view, per-workbook theme and catalog assets.
Every route here requires a logged-in user."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.api.common import render
from app.auth import Forbidden, Identity, get_current_user
from app.csv_export import build_csv, csv_response
from app.models.catalog import ID_PATTERN, is_safe_asset_path
from app.models.users import User
from app.progress import (
    TASK_STATES,
    last_comment_role,
    needs_attention,
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
    german_date,
    local_now,
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
    open_checks: int  # tasks to check plus open questions
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
                    open_checks=sum(needs_attention(progress.tasks.get(tid)) for tid in mine),
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


def display_names(request: Request) -> dict[str, str]:
    """username -> display name, for authors of messages and reviews."""
    return {name: u.name for name, u in request.app.state.users.all().items()}


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
        names=display_names(request),
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
    names = display_names(request)
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
        names=names,
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
    names = display_names(request)
    if identity.is_trainer and username:
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
        names=names,
    )
    filename = f"arbeitsheft_{workbook_id}_{username or 'leer'}_{local_now():%Y-%m-%d}.html"
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
    supervisor_id: str  # username, "" when nobody is assigned
    comment: str
    link: str
    apprentice: str = ""  # display name of the apprentice
    username: str = ""  # the apprentice
    workbook: str = ""  # workbook title
    reviewed_by: str = ""  # display name
    reviewed_at: object = None
    last_comment: str | None = None  # role of the last thread message

    def csv_values(self) -> list[object]:
        """One CSV line, in the order of ``app.csv_export.COLUMNS``."""
        return [self.apprentice, self.username, self.workbook, self.module, self.number,
                self.title, self.state_label, german_date(self.done_at), self.reviewed_by,
                german_date(self.reviewed_at), self.comment, self.supervisor]  # fmt: skip


@dataclass
class TaskGroup:
    title: str  # workbook title (apprentice) or "<apprentice> · <workbook>" (trainer)
    link: str
    rows: list[TaskRow]
    done: int
    total: int


STATE_KEYS = {state: f"my_tasks.state_{state}" for state in TASK_STATES}


def task_rows(
    request: Request,
    view: WorkbookView,
    progress,
    username: str,
    user: User,
    *,
    review_link: bool,
    task_ids: set[str] | None = None,
) -> list[TaskRow]:
    """All (or the given) tasks of ``username``'s workbook with status and Fachbetreuer.

    ``review_link``: link to the trainer review view instead of the own workbook.
    """
    t = request.app.state.translator
    names = {n: u.name for n, u in request.app.state.users.all().items()}
    rows = []
    wid = view.meta.id
    base = f"/workbooks/{wid}/users/{username}" if review_link else f"/workbooks/{wid}"
    for day in view.days:
        for module in day.modules:
            for tv in module.tasks:
                if task_ids is not None and tv.task.id not in task_ids:
                    continue
                tp = progress.tasks.get(tv.task.id)
                state = task_state(tp)
                sup = user.supervisor_for(wid, tv.task.id)
                review = tp.review if tp else None
                reviewer = review.reviewed_by if review else None
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
                        supervisor_id=sup or "",
                        comment=review.comment if review else "",
                        link=f"{base}#task-{tv.task.id}",
                        apprentice=user.name,
                        username=username,
                        workbook=view.meta.title,
                        reviewed_by=names.get(reviewer, reviewer) if reviewer else "",
                        reviewed_at=review.reviewed_at if review else None,
                        last_comment=last_comment_role(tp),
                    )
                )
    return rows


# Filter -> task states shown. "open" means "work to do" for the role; for the
# Fachbetreuer it also includes tasks with an open question (see my_tasks()).
TASK_FILTERS = {
    "apprentice": {"open": {"open", "redo"}, "ok": {"ok"}, "redo": {"redo"}},
    "trainer": {"open": {"waiting", "recheck"}, "ok": {"ok"}, "redo": {"redo"}},
}
NO_SUPERVISOR = "-"  # value of the supervisor filter for tasks without Fachbetreuer


@dataclass
class FilterLink:
    label: str
    url: str
    current: bool


def _url(path: str = "/my-tasks", **params: str) -> str:
    defaults = {"filter": "all", "scope": "mine"}
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items() if v and v != defaults.get(k))
    return path + (f"?{query}" if query else "")


def _apprentice_groups(request: Request, identity: Identity) -> list[TaskGroup]:
    registry = request.app.state.registry
    store = request.app.state.progress
    groups = []
    for wid in registry.catalogs():
        view = registry.view(wid) if identity.user.may_open(wid) else None
        if view is None:
            continue
        progress = store.load(wid, identity.username)
        groups.append(
            TaskGroup(
                title=view.meta.title,
                link=f"/workbooks/{wid}",
                rows=task_rows(
                    request, view, progress, identity.username, identity.user, review_link=False
                ),
                done=progress.done_count(set(view.tasks_by_id)),
                total=view.task_count,
            )
        )
    return groups


def _trainer_groups(request: Request, identity: Identity, everyone: bool) -> list[TaskGroup]:
    store = request.app.state.progress
    registry = request.app.state.registry
    groups = []
    if not everyone:
        for a in assignments_for(request, identity.username):
            progress = store.load(a.view.meta.id, a.username)
            rows = task_rows(
                request,
                a.view,
                progress,
                a.username,
                a.user,
                review_link=True,
                task_ids=set(a.tasks),
            )
            groups.append(
                TaskGroup(
                    title=f"{a.user.name} · {a.view.meta.title}",
                    link=f"/workbooks/{a.view.meta.id}/users/{a.username}",
                    rows=rows,
                    done=a.pv.done,
                    total=a.pv.total,
                )
            )
        return groups
    users = request.app.state.users.all()
    for username, user in sorted(users.items(), key=lambda item: item[1].name.lower()):
        if user.role != "apprentice" or not user.active:
            continue
        for wid in registry.catalogs():
            view = registry.view(wid) if user.may_open(wid) else None
            if view is None:
                continue
            progress = store.load(wid, username)
            groups.append(
                TaskGroup(
                    title=f"{user.name} · {view.meta.title}",
                    link=f"/workbooks/{wid}/users/{username}",
                    rows=task_rows(request, view, progress, username, user, review_link=True),
                    done=progress.done_count(set(view.tasks_by_id)),
                    total=view.task_count,
                )
            )
    return groups


@dataclass
class MyTasks:
    groups: list[TaskGroup]
    active: str  # status filter in effect
    sup: str  # Fachbetreuer filter in effect ("" = all)
    scope: str  # mine | all
    everyone: bool
    filtered: bool
    sup_links: list[FilterLink]


def my_task_groups(
    request: Request, identity: Identity, filter: str, sup: str, scope: str
) -> MyTasks:
    """The groups and rows of "Meine Aufgaben" for the given filters (HTML and CSV)."""
    t = request.app.state.translator
    role = identity.user.role
    states = TASK_FILTERS[role].get(filter)
    active = filter if states else "all"
    everyone = identity.is_trainer and scope == "all"
    scope = "all" if everyone else "mine"

    if identity.is_trainer:
        groups = _trainer_groups(request, identity, everyone)
    else:
        groups = _apprentice_groups(request, identity)

    # Fachbetreuer filter: apprentices (if more than one is involved) and the
    # "all Fachbetreuer" view of Fachbetreuer.
    names = {n: u.name for n, u in request.app.state.users.all().items()}
    present = sorted(
        {r.supervisor_id for g in groups for r in g.rows},
        key=lambda u: (u == "", names.get(u, u).lower()),
    )
    show_sup_filter = everyone or (not identity.is_trainer and len(present) > 1)
    if not show_sup_filter or (sup and sup != NO_SUPERVISOR and sup not in present):
        sup = ""
    sup_links: list[FilterLink] = []
    if show_sup_filter:
        sup_links.append(
            FilterLink(t("my_tasks.sup_all"), _url(filter=active, scope=scope), not sup)
        )
        for username in present:
            value = username or NO_SUPERVISOR
            label = names.get(username, username) if username else t("my_tasks.sup_none")
            sup_links.append(
                FilterLink(label, _url(filter=active, sup=value, scope=scope), sup == value)
            )

    filtered = bool(states or sup)
    questions = identity.is_trainer and active == "open"
    for g in groups:
        if states:
            g.rows = [
                r
                for r in g.rows
                if r.state in states or (questions and r.last_comment == "apprentice")
            ]
        if sup:
            wanted = "" if sup == NO_SUPERVISOR else sup
            g.rows = [r for r in g.rows if r.supervisor_id == wanted]
    if filtered:
        groups = [g for g in groups if g.rows]
    return MyTasks(groups, active, sup, scope, everyone, filtered, sup_links)


@router.get("/my-tasks", response_class=HTMLResponse)
def my_tasks(
    request: Request,
    identity: CurrentUser,
    filter: str = "all",
    sup: str = "",
    scope: str = "mine",
) -> HTMLResponse:
    t = request.app.state.translator
    mt = my_task_groups(request, identity, filter, sup, scope)
    active, sup, scope, everyone = mt.active, mt.sup, mt.scope, mt.everyone
    open_label = "my_tasks.filter_open_trainer" if identity.is_trainer else "my_tasks.filter_open"
    status_links = [
        FilterLink(t(label), _url(filter=key, sup=sup, scope=scope), active == key)
        for key, label in (("all", "my_tasks.filter_all"), ("open", open_label),
                           ("ok", "my_tasks.filter_ok"), ("redo", "my_tasks.filter_redo"))
    ]  # fmt: skip
    scope_links = []
    if identity.is_trainer:
        scope_links = [
            FilterLink(t("my_tasks.scope_mine"), _url(filter=active), not everyone),
            FilterLink(t("my_tasks.scope_all"), _url(filter=active, scope="all"), everyone),
        ]
    return render(
        request,
        "my_tasks.html",
        groups=mt.groups,
        active=active,
        filtered=mt.filtered,
        everyone=everyone,
        status_links=status_links,
        sup_links=mt.sup_links,
        scope_links=scope_links,
        csv_url=_url("/my-tasks.csv", filter=active, sup=sup, scope=scope),
    )


@router.get("/my-tasks.csv")
def my_tasks_csv(
    request: Request,
    identity: CurrentUser,
    filter: str = "all",
    sup: str = "",
    scope: str = "mine",
) -> Response:
    """Exactly the rows of the current "Meine Aufgaben" view as CSV download."""
    mt = my_task_groups(request, identity, filter, sup, scope)
    rows = [r.csv_values() for g in mt.groups for r in g.rows]
    content = build_csv(request.app.state.translator, rows)
    return csv_response(content, f"meine_aufgaben_{local_now():%Y-%m-%d}.csv")
