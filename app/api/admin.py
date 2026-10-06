"""Admin pages for Fachbetreuer (role ``trainer``): catalogs and users."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.common import render
from app.auth import Identity, is_valid_username, require_trainer, suggest_username, verify_form
from app.i18n import Translator
from app.loader import CatalogError, Entry
from app.models.catalog import ID_PATTERN
from app.models.users import User
from app.progress import needs_check
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
                "error": None, "invite": None}  # fmt: skip
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
