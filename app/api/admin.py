"""Trainer/admin pages. In 0.2.0 only /admin/catalogs exists (no auth yet;
the app is bound to localhost)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.i18n import Translator
from app.loader import CatalogError, Entry
from app.theme import contrast_warnings

router = APIRouter(prefix="/admin")

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
    html = request.app.state.templates.get_template("admin_catalogs.html").render(rows=rows)
    return HTMLResponse(html)
