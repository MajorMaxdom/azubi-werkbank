"""Admin pages for Fachbetreuer (role ``trainer``): catalogs and users."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.account import data_zip_response
from app.api.common import render
from app.api.pages import task_rows
from app.auth import Identity, is_valid_username, require_trainer, suggest_username, verify_form
from app.csv_export import build_csv, csv_response
from app.i18n import Translator
from app.loader import CatalogError, Entry
from app.models.catalog import ID_PATTERN
from app.models.users import User
from app.progress import CorruptProgress, needs_check
from app.renderer import german_date
from app.theme import contrast_warnings

router = APIRouter(prefix="/admin", dependencies=[Depends(require_trainer)])

_ID_RE = re.compile(ID_PATTERN)
Trainer = Annotated[Identity, Depends(require_trainer)]

_PATTERN_FIELDS = {"id": "id", "duration": "duration", "version": "version"}


def localize_error(error: CatalogError, t: Translator) -> str:
    """German message for a catalog error; the key path stays English."""
    ctx = dict(error.ctx)
    try:
        if error.type == "string_pattern_mismatch":
            return t(f"errors.string_pattern_mismatch.{_pattern_kind(error)}")
        if error.type == "duplicate_id":
            kind = ctx.get("kind", "")
            ctx["kind"] = t(f"errors.kind.{kind}") if kind else ""
        if error.type in ("parse_error", "read_error"):
            ctx["message"] = error.message
        return t(f"errors.{error.type}", **ctx)
    except (KeyError, IndexError, ValueError):
        return t("errors.generic", message=error.message)


def _pattern_kind(error: CatalogError) -> str:
    if "stylesheet" in error.loc:
        return "color"
    last = next((p for p in reversed(error.loc) if isinstance(p, str)), "")
    return _PATTERN_FIELDS.get(last, "other")


@dataclass
class CatalogRow:
    entry: Entry
    status: str  # ok | error | stale
    status_label: str
    errors: list[tuple[CatalogError, str]]
    warnings: list[str]


def build_rows(entries: list[Entry], t: Translator) -> list[CatalogRow]:
    rows = []
    for entry in entries:
        errors = [(e, localize_error(e, t)) for e in entry.all_errors]
        if not errors:
            status = "ok"
        elif entry.catalog is not None and entry.conflict is None:
            status = "stale"
        else:
            status = "error"
        warnings = []
        if entry.catalog is not None:
            for w in contrast_warnings(entry.catalog.workbook.stylesheet):
                warnings.append(
                    t(
                        "admin.catalogs.contrast",
                        ratio=f"{w.ratio:.2f}".replace(".", ","),
                        foreground=w.foreground,
                        background=w.background,
                    )
                )
        label = {
            "ok": t("admin.catalogs.status_ok"),
            "stale": t("admin.catalogs.status_stale"),
            "error": t("admin.catalogs.status_error"),
        }[status]
        rows.append(CatalogRow(entry, status, label, errors, warnings))
    return rows


@router.get("/catalogs", response_class=HTMLResponse)
def catalogs(request: Request) -> HTMLResponse:
    rows = build_rows(request.app.state.registry.entries(), request.app.state.translator)
    return render(request, "admin_catalogs.html", rows=rows)


# --------------------------------------------------------------------------- users


@dataclass
class UserRow:
    username: str
    user: User
    status: str
    status_label: str
    warnings: list[str]


def supervisor_warnings(request: Request, username: str, user: User) -> list[str]:
    """Problems in the Fachbetreuer assignment and workbook list (warnings only)."""
    t = request.app.state.translator
    users = request.app.state.users.all()
    catalogs = request.app.state.registry.catalogs()
    warnings: list[str] = []
    for wid in user.workbooks or []:
        if wid not in catalogs:
            warnings.append(t("admin.users.warn_workbook", workbook=wid))
    for wid, supervision in user.supervisors.items():
        catalog = catalogs.get(wid)
        if catalog is None:
            warnings.append(t("admin.users.warn_workbook", workbook=wid))
        task_ids = (
            {task.id for day in catalog.days for module in day.modules for task in module.tasks}
            if catalog
            else set()
        )
        names = [supervision.default] if supervision.default else []
        for task_id, name in supervision.tasks.items():
            names.append(name)
            if catalog is not None and task_id not in task_ids:
                warnings.append(t("admin.users.warn_task", workbook=wid, task=task_id))
        for name in names:
            other = users.get(name)
            if other is None or other.role != "trainer":
                warnings.append(t("admin.users.warn_supervisor", user=name))
    if user.role == "trainer" and user.supervisors:
        warnings.append(t("admin.users.warn_trainer_supervisors"))
    return warnings


USER_STATUS_KEYS = {
    "active": "admin.users.status_active",
    "invited": "admin.users.status_invited",
    "invite_expired": "admin.users.status_invite_expired",
    "no_access": "admin.users.status_no_access",
    "inactive": "admin.users.status_inactive",
}


def user_rows(request: Request) -> list[UserRow]:
    accounts = request.app.state.accounts
    t = request.app.state.translator
    rows = []
    for username, user in sorted(request.app.state.users.all().items()):
        status = accounts.status(username)
        rows.append(
            UserRow(
                username=username,
                user=user,
                status=status,
                status_label=t(USER_STATUS_KEYS[status]),
                warnings=supervisor_warnings(request, username, user),
            )
        )
    return rows


def page(request: Request, status_code: int = 200, **context) -> HTMLResponse:
    defaults = {"form": {"name": "", "username": "", "role": "apprentice", "workbooks": []},
                "error": None, "invite": None, "deleted": None}  # fmt: skip
    defaults.update(context)
    return render(
        request,
        "admin_users.html",
        status_code=status_code,
        rows=user_rows(request),
        users_error=request.app.state.users.error,
        catalogs=request.app.state.registry.catalogs(),
        **defaults,
    )


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request) -> HTMLResponse:
    return page(request)


@router.post("/users", dependencies=[Depends(verify_form)], response_class=HTMLResponse)
def create_user(
    request: Request,
    name: Annotated[str, Form(max_length=200)] = "",
    username: Annotated[str, Form(max_length=200)] = "",
    role: Annotated[str, Form(max_length=20)] = "apprentice",
    workbooks: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    name = name.strip()
    username = username.strip().lower()
    selected = [w for w in (workbooks or []) if _ID_RE.fullmatch(w)]
    form = {"name": name, "username": username, "role": role, "workbooks": selected}
    existing = request.app.state.users.all()
    error = None
    if not name:
        error = "admin.users.error_name"
    elif role not in ("trainer", "apprentice"):
        error = "admin.users.error_role"
    if not username and not error:
        username = suggest_username(name, existing)
    if not error and not is_valid_username(username):
        error = "admin.users.error_username"
    elif not error and username in existing:
        error = "admin.users.error_exists"
    if error:
        return page(request, status_code=400, form=form, error=error)
    link = request.app.state.accounts.create_user(
        username, name, role, selected if role == "apprentice" and selected else None
    )
    return page(request, invite={"username": username, "name": name, "link": link})


def _target(request: Request, username: str, identity: Identity) -> str:
    if not is_valid_username(username) or request.app.state.users.get(username) is None:
        raise HTTPException(status_code=404)
    return username


@router.post(
    "/users/{username}/reset", dependencies=[Depends(verify_form)], response_class=HTMLResponse
)
def reset_user(request: Request, username: str, identity: Trainer) -> HTMLResponse:
    username = _target(request, username, identity)
    link = request.app.state.accounts.reset_access(username)
    user = request.app.state.users.get(username)
    return page(request, invite={"username": username, "name": user.name, "link": link})


@router.post("/users/{username}/deactivate", dependencies=[Depends(verify_form)])
def deactivate_user(request: Request, username: str, identity: Trainer):
    username = _target(request, username, identity)
    if username == identity.username:
        return page(request, status_code=400, error="admin.users.error_self")
    request.app.state.accounts.set_active(username, False)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{username}/activate", dependencies=[Depends(verify_form)])
def activate_user(request: Request, username: str, identity: Trainer) -> RedirectResponse:
    username = _target(request, username, identity)
    request.app.state.accounts.set_active(username, True)
    return RedirectResponse("/admin/users", status_code=303)


# --------------------------------------------------------------------------- data export & delete


@router.get("/users/{username}/data")
def user_data(request: Request, username: str, identity: Trainer) -> Response:
    username = _target(request, username, identity)
    return data_zip_response(request, username)


@dataclass
class ProgressSummary:
    workbook_id: str
    title: str
    tasks: int | None  # tasks with saved data; None if the file is unreadable
    done: int | None


@dataclass
class DeleteSummary:
    has_credentials: bool
    progress: list[ProgressSummary]
    supervised: list[tuple[str, str, int]]  # (username, name, assignments)


def delete_summary(request: Request, username: str) -> DeleteSummary:
    state = request.app.state
    catalogs = state.registry.catalogs()
    progress = []
    for wid, _path in state.progress.files_for(username):
        catalog = catalogs.get(wid)
        title = catalog.workbook.title if catalog else wid
        try:
            data = state.progress.load(wid, username)
        except CorruptProgress:
            progress.append(ProgressSummary(wid, title, None, None))
            continue
        done = sum(1 for tp in data.tasks.values() if tp.done)
        progress.append(ProgressSummary(wid, title, len(data.tasks), done))
    supervised = []
    for other, user in sorted(state.users.all().items()):
        count = 0
        for supervision in user.supervisors.values():
            count += supervision.default == username
            count += sum(1 for name in supervision.tasks.values() if name == username)
        if count and other != username:
            supervised.append((other, user.name, count))
    return DeleteSummary(
        has_credentials=state.credentials.get(username) is not None,
        progress=progress,
        supervised=supervised,
    )


def delete_page(
    request: Request, username: str, status_code: int = 200, error: str | None = None
) -> HTMLResponse:
    return render(
        request,
        "admin_user_delete.html",
        status_code=status_code,
        username=username,
        user=request.app.state.users.get(username),
        summary=delete_summary(request, username),
        error=error,
    )


@router.get("/users/{username}/delete", response_class=HTMLResponse)
def confirm_delete(request: Request, username: str, identity: Trainer) -> HTMLResponse:
    username = _target(request, username, identity)
    if username == identity.username:
        return page(request, status_code=400, error="user_delete.error_self")
    return delete_page(request, username)


@router.post(
    "/users/{username}/delete", dependencies=[Depends(verify_form)], response_class=HTMLResponse
)
def delete_user(
    request: Request,
    username: str,
    identity: Trainer,
    confirm: Annotated[str, Form(max_length=200)] = "",
) -> HTMLResponse:
    username = _target(request, username, identity)
    if username == identity.username:
        return page(request, status_code=400, error="user_delete.error_self")
    if confirm.strip() != username:
        return delete_page(request, username, status_code=400, error="user_delete.error_confirm")
    name = request.app.state.users.get(username).name
    request.app.state.accounts.delete_user(
        username, request.app.state.progress, by=identity.username
    )
    return page(request, deleted={"username": username, "name": name})


# --------------------------------------------------------------------------- edit apprentice


def _trainers(request: Request) -> list[tuple[str, User]]:
    users = request.app.state.users.all()
    return sorted(
        ((name, u) for name, u in users.items() if u.role == "trainer"),
        key=lambda item: item[1].name.lower(),
    )


def edit_page(
    request: Request, username: str, user: User, status_code: int = 200, **context
) -> HTMLResponse:
    registry = request.app.state.registry
    views = [registry.view(wid) for wid in registry.catalogs()]
    return render(
        request,
        "admin_user_edit.html",
        status_code=status_code,
        username=username,
        user=user,
        views=[v for v in views if v is not None],
        trainers=_trainers(request),
        error=context.get("error"),
        saved=context.get("saved", False),
    )


def _apprentice(request: Request, username: str) -> User:
    if not is_valid_username(username):
        raise HTTPException(status_code=404)
    user = request.app.state.users.get(username)
    if user is None:
        raise HTTPException(status_code=404)
    return user


@router.get("/users/{username}", response_class=HTMLResponse)
def edit_user(request: Request, username: str) -> HTMLResponse:
    user = _apprentice(request, username)
    if user.role != "apprentice":
        raise HTTPException(status_code=404)
    return edit_page(request, username, user)


@router.post("/users/{username}", dependencies=[Depends(verify_form)], response_class=HTMLResponse)
async def save_user(request: Request, username: str) -> HTMLResponse:
    user = _apprentice(request, username)
    if user.role != "apprentice":
        raise HTTPException(status_code=404)
    form = await request.form()
    catalogs = request.app.state.registry.catalogs()
    trainers = {name for name, _ in _trainers(request)}

    name = str(form.get("name", "")).strip()[:200]
    if not name:
        return edit_page(request, username, user, status_code=400, error="admin.users.error_name")

    selected = [w for w in form.getlist("workbooks") if isinstance(w, str) and w in catalogs]
    allowed = selected or list(catalogs)  # no selection = all workbooks

    supervisors: dict[str, tuple[str | None, dict[str, str]]] = {}
    for wid in allowed:
        catalog = catalogs[wid]
        default = str(form.get(f"supervisor:{wid}", "") or "")
        if default and default not in trainers:
            return edit_page(
                request, username, user, status_code=400, error="admin.users.error_supervisor"
            )
        tasks: dict[str, str] = {}
        for day in catalog.days:
            for module in day.modules:
                for task in module.tasks:
                    value = str(form.get(f"task:{wid}:{task.id}", "") or "")
                    if not value or value == default:
                        continue
                    if value not in trainers:
                        return edit_page(
                            request,
                            username,
                            user,
                            status_code=400,
                            error="admin.users.error_supervisor",
                        )
                    tasks[task.id] = value
        supervisors[wid] = (default or None, tasks)

    request.app.state.users.update_apprentice(
        username,
        name=name,
        workbooks=selected or None,
        supervisors=supervisors,
        managed=list(catalogs),
    )
    return edit_page(request, username, request.app.state.users.get(username), saved=True)


# --------------------------------------------------------------------------- overview


@dataclass
class Cell:
    task_id: str
    number: str
    title: str
    state: str  # open | done | ok | redo
    label: str


@dataclass
class OverviewRow:
    username: str
    user: User
    modules: list[tuple[str, list[Cell]]]
    done: int
    total: int
    open_checks: int
    supervisor: str


@dataclass
class OverviewSection:
    view: object
    rows: list[OverviewRow]


def _cell_state(tp) -> str:
    if tp is not None and tp.review is not None and tp.review.status == "redo":
        return "redo"
    if tp is not None and tp.done:
        if tp.review is not None and tp.review.status == "ok" and not needs_check(tp):
            return "ok"
        return "done"
    return "open"


def overview_sections(request: Request) -> list[OverviewSection]:
    t = request.app.state.translator
    registry = request.app.state.registry
    store = request.app.state.progress
    users = request.app.state.users.all()
    names = {name: u.name for name, u in users.items()}
    labels = {
        "open": t("overview.state_open"),
        "done": t("overview.state_done"),
        "ok": t("overview.state_ok"),
        "redo": t("overview.state_redo"),
    }
    sections = []
    for wid in registry.catalogs():
        view = registry.view(wid)
        if view is None:
            continue
        rows = []
        for username, user in sorted(users.items(), key=lambda item: item[1].name.lower()):
            if user.role != "apprentice" or not user.active or not user.may_open(wid):
                continue
            progress = store.load(wid, username)
            modules = []
            for day in view.days:
                for module in day.modules:
                    cells = []
                    for tv in module.tasks:
                        tp = progress.tasks.get(tv.task.id)
                        state = _cell_state(tp)
                        label = f"{tv.number} {tv.task.title} – {labels[state]}"
                        if tp is not None and tp.done_at and state != "open":
                            label += " " + t("overview.done_at", date=german_date(tp.done_at))
                        cells.append(Cell(tv.task.id, tv.number, tv.task.title, state, label))
                    modules.append((module.module.code, cells))
            task_ids = list(view.tasks_by_id)
            supervision = user.supervisors.get(wid)
            default = supervision.default if supervision else None
            rows.append(
                OverviewRow(
                    username=username,
                    user=user,
                    modules=modules,
                    done=progress.done_count(set(task_ids)),
                    total=len(task_ids),
                    open_checks=sum(needs_check(progress.tasks.get(tid)) for tid in task_ids),
                    supervisor=names.get(default, default) if default else "",
                )
            )
        sections.append(OverviewSection(view=view, rows=rows))
    return sections


@router.get("/overview", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    return render(request, "admin_overview.html", sections=overview_sections(request))


def _active_apprentices(request: Request, workbook_id: str) -> list[tuple[str, User]]:
    """Active apprentices who may open the workbook, sorted by display name."""
    users = request.app.state.users.all()
    return sorted(
        (
            (name, u)
            for name, u in users.items()
            if u.role == "apprentice" and u.active and u.may_open(workbook_id)
        ),
        key=lambda item: (item[1].name.lower(), item[0]),
    )


@router.get("/overview.csv")
def overview_csv(request: Request, workbook: str | None = None) -> Response:
    """All tasks of all active apprentices (optionally of one workbook) as CSV."""
    registry = request.app.state.registry
    if workbook is not None:
        if not _ID_RE.fullmatch(workbook) or registry.view(workbook) is None:
            raise HTTPException(status_code=404)
        workbook_ids = [workbook]
    else:
        workbook_ids = list(registry.catalogs())
    store = request.app.state.progress
    rows = []
    for wid in workbook_ids:
        view = registry.view(wid)
        if view is None:
            continue
        for username, user in _active_apprentices(request, wid):
            progress = store.load(wid, username)
            for row in task_rows(request, view, progress, username, user, review_link=True):
                rows.append(row.csv_values())
    content = build_csv(request.app.state.translator, rows)
    filename = f"uebersicht_{workbook or 'alle'}_{datetime.now(UTC):%Y-%m-%d}.csv"
    return csv_response(content, filename)


# --------------------------------------------------------------------------- bulk assignment

KEEP = ""  # select value: leave unchanged
NOBODY = "-"  # workbook select: no Fachbetreuer
SAME_AS_WORKBOOK = "="  # module select: remove the task overrides of the module


@dataclass
class AssignRow:
    username: str
    user: User
    supervisor: str  # display name of the workbook default, "" = nobody
    overrides: int
    checked: bool


def assign_page(
    request: Request,
    workbook_id: str | None,
    status_code: int = 200,
    *,
    error: str | None = None,
    saved: int | None = None,
    form: dict | None = None,
) -> HTMLResponse:
    registry = request.app.state.registry
    views = [v for v in (registry.view(wid) for wid in registry.catalogs()) if v is not None]
    view = registry.view(workbook_id) if workbook_id else None
    form = form or {"users": [], "default": KEEP, "modules": {}}
    names = {n: u.name for n, u in request.app.state.users.all().items()}
    rows = []
    modules = []
    if view is not None:
        for username, user in _active_apprentices(request, workbook_id):
            supervision = user.supervisors.get(workbook_id)
            default = supervision.default if supervision else None
            rows.append(
                AssignRow(
                    username=username,
                    user=user,
                    supervisor=names.get(default, default) if default else "",
                    overrides=len(supervision.tasks) if supervision else 0,
                    checked=username in form["users"],
                )
            )
        modules = [m.module for day in view.days for m in day.modules]
    return render(
        request,
        "admin_assign.html",
        status_code=status_code,
        views=views,
        view=view,
        rows=rows,
        modules=modules,
        trainers=_trainers(request),
        form=form,
        error=error,
        saved=saved,
        keep=KEEP,
        nobody=NOBODY,
        same_as_workbook=SAME_AS_WORKBOOK,
    )


@router.get("/assign", response_class=HTMLResponse)
def assign(request: Request, workbook: str | None = None) -> HTMLResponse:
    registry = request.app.state.registry
    if workbook is None:
        return assign_page(request, next(iter(registry.catalogs()), None))
    if not _ID_RE.fullmatch(workbook) or registry.view(workbook) is None:
        return assign_page(request, None, status_code=404, error="assign.error_workbook")
    return assign_page(request, workbook)


@router.post("/assign", dependencies=[Depends(verify_form)], response_class=HTMLResponse)
async def save_assign(request: Request) -> HTMLResponse:
    form = await request.form()
    registry = request.app.state.registry
    workbook_id = str(form.get("workbook", ""))
    view = registry.view(workbook_id) if _ID_RE.fullmatch(workbook_id) else None
    if view is None:
        return assign_page(request, None, status_code=400, error="assign.error_workbook")

    usernames = list(dict.fromkeys(str(u) for u in form.getlist("users")))
    default = str(form.get("default", KEEP) or KEEP)
    module_values: dict[str, str] = {}
    for day in view.days:
        for m in day.modules:
            module_values[m.module.code] = str(form.get(f"module:{m.module.code}", KEEP) or KEEP)
    state = {"users": usernames, "default": default, "modules": module_values}

    def fail(key: str) -> HTMLResponse:
        return assign_page(request, workbook_id, status_code=400, error=key, form=state)

    users = request.app.state.users.all()
    trainers = {name for name, _ in _trainers(request)}
    if not usernames:
        return fail("assign.error_no_users")
    for username in usernames:
        user = users.get(username) if is_valid_username(username) else None
        if user is None or user.role != "apprentice" or not user.may_open(workbook_id):
            return fail("assign.error_user")
    if default not in (KEEP, NOBODY) and default not in trainers:
        return fail("assign.error_supervisor")
    for value in module_values.values():
        if value not in (KEEP, SAME_AS_WORKBOOK) and value not in trainers:
            return fail("assign.error_supervisor")

    tasks: dict[str, str | None] = {}
    for day in view.days:
        for m in day.modules:
            value = module_values[m.module.code]
            if value == KEEP:
                continue
            for tv in m.tasks:
                tasks[tv.task.id] = None if value == SAME_AS_WORKBOOK else value
    if default == KEEP and not tasks:
        return fail("assign.error_nothing")

    directory = request.app.state.users
    if default == KEEP:
        directory.assign_supervisors(workbook_id, usernames, tasks=tasks)
    else:
        new_default = None if default == NOBODY else default
        directory.assign_supervisors(workbook_id, usernames, default=new_default, tasks=tasks)
    return assign_page(request, workbook_id, saved=len(usernames))
