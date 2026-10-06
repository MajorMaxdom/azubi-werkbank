"""Turn validated catalogs into HTML: markdown, durations, day load, numbering,
task hashes, per-workbook theme CSS and the Jinja2 environment."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from app.i18n import ROOT, Translator, get_translator
from app.models.catalog import DURATION_PATTERN, Catalog, Day, Module, Task
from app.models.progress import Progress, TaskProgress
from app.theme import theme_css, theme_hash

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# --------------------------------------------------------------------------- markdown

# CommonMark with raw HTML disabled: "<b>" in catalog text is shown literally.
_md = MarkdownIt("commonmark", {"html": False})


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def render_markdown_inline(text: str | None) -> Markup:
    return Markup(_md.renderInline(text or ""))


# --------------------------------------------------------------------------- durations

_DURATION_RE = re.compile(DURATION_PATTERN)


def parse_duration(value: str | None) -> int:
    """Return minutes for a duration like ``45m``, ``1h`` or ``1h30m`` (None -> 0)."""
    if not value:
        return 0
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid duration: {value!r}")
    hours, minutes = match.groups()
    total = int(hours[:-1]) * 60 if hours else 0
    total += int(minutes[:-1]) if minutes else 0
    return total


def format_hours(minutes: int) -> str:
    """German hour figure with one decimal, e.g. 150 -> "2,5", 180 -> "3", 195 -> "3,2"."""
    hours = (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
    text = f"{hours:f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text.replace(".", ",")


def day_load_text(task_count: int, minutes: int, t: Translator) -> str:
    tasks = t("day.tasks", count=task_count)
    if not minutes:
        return tasks
    return t("day.load", tasks=tasks, hours=format_hours(minutes))


# --------------------------------------------------------------------------- hashing


def task_hash(task: Task) -> str:
    """SHA-256 of the normalized task content, excluding trainer-only data."""
    payload = task.model_dump(mode="json", exclude={"trainer"})
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- view model


@dataclass
class TaskView:
    task: Task
    number: str
    minutes: int
    hash: str


@dataclass
class ModuleView:
    module: Module
    tasks: list[TaskView]


@dataclass
class DayView:
    day: Day
    index: int
    label: str
    nav_tag: str
    nav_label: str
    task_count: int
    minutes: int
    load: str
    modules: list[ModuleView]


@dataclass
class WorkbookView:
    catalog: Catalog
    days: list[DayView]
    task_count: int
    theme_css: str
    theme_hash: str
    tasks_by_id: dict[str, TaskView] = field(default_factory=dict)

    @property
    def meta(self):
        return self.catalog.workbook


def build_view(catalog: Catalog, t: Translator | None = None) -> WorkbookView:
    t = t or get_translator()
    days: list[DayView] = []
    by_id: dict[str, TaskView] = {}
    for d_index, day in enumerate(catalog.days, start=1):
        nav_tag = day.nav_tag or str(d_index)
        number_prefix = nav_tag if day.optional else str(d_index)
        modules: list[ModuleView] = []
        position = 0
        minutes = 0
        for module in day.modules:
            views: list[TaskView] = []
            for task in module.tasks:
                position += 1
                view = TaskView(
                    task=task,
                    number=task.number or f"{number_prefix}.{position}",
                    minutes=parse_duration(task.duration),
                    hash=task_hash(task),
                )
                minutes += view.minutes
                views.append(view)
                by_id[task.id] = view
            modules.append(ModuleView(module=module, tasks=views))
        days.append(
            DayView(
                day=day,
                index=d_index,
                label=t("day.optional") if day.optional else t("day.label", n=d_index),
                nav_tag=nav_tag,
                nav_label=day.nav_label or day.title,
                task_count=position,
                minutes=minutes,
                load=day_load_text(position, minutes, t),
                modules=modules,
            )
        )
    css = theme_css(catalog.workbook.stylesheet)
    return WorkbookView(
        catalog=catalog,
        days=days,
        task_count=len(by_id),
        theme_css=css,
        theme_hash=theme_hash(css),
        tasks_by_id=by_id,
    )


# --------------------------------------------------------------------------- templates


@cache
def _icon_svg(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"Invalid icon name: {name!r}")
    svg = (STATIC_DIR / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r"<!--.*?-->\s*", "", svg, flags=re.S)
    svg = re.sub(r"\s+", " ", svg).strip()
    return svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ', 1).replace(
        'class="lucide ', 'class="icon lucide ', 1
    )


def icon(name: str) -> Markup:
    return Markup(_icon_svg(name))


def create_environment(t: Translator | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["t"] = t or get_translator()
    env.globals["icon"] = icon
    env.filters["md"] = render_markdown
    env.filters["md_inline"] = render_markdown_inline
    env.filters["textarea_rows"] = textarea_rows
    env.filters["de_date"] = german_date
    return env


def german_date(value) -> str:
    """ISO date or datetime -> ``dd.mm.yyyy`` (local time for datetimes)."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone().strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    try:
        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def textarea_rows(height: int | None) -> int:
    """Approximate a min-height in px as textarea rows (inline styles are forbidden by CSP)."""
    if not height:
        return 4
    return max(2, round(height / 24))


# --------------------------------------------------------------------------- static output

_CSS_URL_RE = re.compile(r"url\(\"?(\.\./fonts/[A-Za-z0-9._-]+)\"?\)")


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".woff2":
        mime = "font/woff2"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def inline_base_css() -> str:
    """fonts.css (with fonts embedded) + tokens.css + app.css, for offline files."""
    css_dir = STATIC_DIR / "css"
    parts = []
    for name in ("fonts.css", "tokens.css", "app.css"):
        css = (css_dir / name).read_text(encoding="utf-8")
        css = _CSS_URL_RE.sub(lambda m: f'url("{_data_uri(css_dir / m.group(1))}")', css)
        parts.append(css)
    return "\n".join(parts)


def static_asset_url(workbooks_dir: Path):
    """Asset resolver for offline files: embed images as data URIs when present."""

    def resolve(src: str) -> str:
        path = (workbooks_dir / src).resolve()
        assets = (workbooks_dir / "assets").resolve()
        if path.is_relative_to(assets) and path.is_file():
            return _data_uri(path)
        return src

    return resolve


@dataclass
class ReviewContext:
    """The apprentice a Fachbetreuer is looking at (review view and trainer export)."""

    username: str
    user: Any
    supervisors: dict[str, str]  # task id -> Fachbetreuer display name
    names: dict[str, str]  # username -> display name
    orphans: list

    def name(self, username: str | None) -> str:
        if not username:
            return ""
        return self.names.get(username, username)


@dataclass
class ExportInfo:
    """Header data of an exported snapshot."""

    name: str  # whose workbook (display name), empty for a blank workbook
    username: str
    exported_at: datetime


def render_static(
    catalog: Catalog,
    *,
    trainer: bool = True,
    workbooks_dir: Path | None = None,
    t: Translator | None = None,
    progress: Progress | None = None,
    export: ExportInfo | None = None,
    review: ReviewContext | None = None,
    view: WorkbookView | None = None,
) -> str:
    """Render a self-contained HTML file with tokens, theme and fonts inlined.

    Without ``export`` this is the authoring preview (``workbook render``).
    With ``export`` it is a print-friendly snapshot of one user's answers.
    """
    env = create_environment(t)
    view = view or build_view(catalog, t)
    template = env.get_template("workbook.html")
    return template.render(
        wb=view,
        is_trainer=trainer,
        static=True,
        export=export,
        review=review,
        inline_css=Markup(inline_base_css()),
        inline_theme=Markup(view.theme_css),
        asset_url=static_asset_url(workbooks_dir or ROOT / "workbooks"),
        pv=build_progress_view(view, progress, editable=False),
    )


# --------------------------------------------------------------------------- progress state


@dataclass
class ProgressView:
    """A user's saved state as seen by the templates (empty when there is none)."""

    progress: Progress | None = None
    editable: bool = False
    total: int = 0
    task_hashes: dict[str, str] = field(default_factory=dict)

    def task(self, task_id: str) -> TaskProgress | None:
        return self.progress.tasks.get(task_id) if self.progress else None

    def answer(self, task_id: str, answer_id: str):
        tp = self.task(task_id)
        return tp.answers.get(answer_id) if tp else None

    def header(self, field_id: str) -> str:
        return self.progress.header.get(field_id, "") if self.progress else ""

    def is_done(self, task_id: str) -> bool:
        tp = self.task(task_id)
        return bool(tp and tp.done)

    def review_status(self, task_id: str) -> str | None:
        tp = self.task(task_id)
        return tp.review.status if tp and tp.review else None

    def changed(self, task_id: str) -> bool:
        """The task changed in the catalog after the user last worked on it."""
        tp = self.task(task_id)
        current = self.task_hashes.get(task_id)
        return bool(tp and tp.task_hash and current and tp.task_hash != current)

    @property
    def done(self) -> int:
        if not self.progress:
            return 0
        return self.progress.done_count(set(self.task_hashes))

    @property
    def percent(self) -> int:
        return round(self.done * 100 / self.total) if self.total else 0


def build_progress_view(
    view: WorkbookView, progress: Progress | None, *, editable: bool
) -> ProgressView:
    return ProgressView(
        progress=progress,
        editable=editable,
        total=view.task_count,
        task_hashes={tid: tv.hash for tid, tv in view.tasks_by_id.items()},
    )
