"""Pydantic models for workbook catalogs (see docs/workbook-template.yaml).

Rules that only need the object itself live in model validators. Rules that need
the whole workbook (unique ids across days, level references) live in
``check_references`` so that errors can point at the exact offending key.
"""

from __future__ import annotations

import posixpath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
HEX_COLOR_PATTERN = r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
DURATION_PATTERN = r"^(\d+h)?(\d+m)?$"
# Official semver regex (https://semver.org), without the optional "v" prefix.
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
HexColor = Annotated[str, StringConstraints(pattern=HEX_COLOR_PATTERN)]
Duration = Annotated[str, StringConstraints(pattern=DURATION_PATTERN, min_length=1)]
SemVer = Annotated[str, StringConstraints(pattern=SEMVER_PATTERN)]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
LevelColor = Literal["blue", "ochre", "green", "grey", "red"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- stylesheet


class _ColorGroup(Strict):
    @model_validator(mode="before")
    @classmethod
    def _reject_nulls(cls, data: Any) -> Any:
        # An unquoted "#123456" in YAML is a comment and silently becomes null.
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    raise PydanticCustomError(
                        "color_null",
                        "Color '{key}' is empty; hex colors must be quoted in YAML, "
                        'e.g. "#1B365D"',
                        {"key": key},
                    )
        return data


class LevelPalette(_ColorGroup):
    blue: HexColor | None = None
    ochre: HexColor | None = None
    green: HexColor | None = None
    grey: HexColor | None = None
    red: HexColor | None = None


class Stylesheet(_ColorGroup):
    paper: HexColor | None = None
    card: HexColor | None = None
    ink: HexColor | None = None
    muted: HexColor | None = None
    line: HexColor | None = None
    accent: HexColor | None = None
    accent_hover: HexColor | None = None
    accent_soft: HexColor | None = None
    ok: HexColor | None = None
    redo: HexColor | None = None
    hint_bg: HexColor | None = None
    trainer_bg: HexColor | None = None
    bonus_bg: HexColor | None = None
    level_palette: LevelPalette | None = None


# --------------------------------------------------------------------------- workbook meta


class HeaderField(Strict):
    id: Id
    label: NonEmpty
    type: Literal["date", "short", "text"] = "short"
    placeholder: str | None = None


class Signoff(Strict):
    enabled: bool = True
    title: str | None = None


class WorkbookMeta(Strict):
    id: Id
    version: SemVer
    title: NonEmpty
    subtitle: str | None = None
    brand: str | None = None
    description: str | None = None
    intro: str | None = None
    header_fields: list[HeaderField] = []
    footer: str | None = None
    signoff: Signoff = Signoff()
    stylesheet: Stylesheet | None = None

    @field_validator("header_fields")
    @classmethod
    def _unique_header_ids(cls, fields: list[HeaderField]) -> list[HeaderField]:
        _ensure_unique([f.id for f in fields], "header field")
        return fields


class Level(Strict):
    label: NonEmpty
    color: LevelColor
    description: str | None = None


# --------------------------------------------------------------------------- answers & blocks


class Answer(Strict):
    id: Id
    type: Literal["text", "short", "checklist", "choice", "date"] = "text"
    label: str | None = None
    placeholder: str | None = None
    height: PositiveInt | None = None
    items: list[NonEmpty] | None = None
    options: list[NonEmpty] | None = None
    multiple: bool = False

    @model_validator(mode="after")
    def _type_requirements(self) -> Answer:
        if self.type == "choice" and not self.options:
            raise PydanticCustomError("choice_options", "Answer type 'choice' needs 'options'")
        if self.type == "checklist" and not self.items:
            raise PydanticCustomError("checklist_items", "Answer type 'checklist' needs 'items'")
        return self


class TextBlock(Strict):
    type: Literal["text"]
    content: NonEmpty


class TableBlock(Strict):
    type: Literal["table"]
    caption: str | None = None
    columns: list[str]
    rows: list[list[str]] = []


class SnippetBlock(Strict):
    type: Literal["snippet"]
    caption: str | None = None
    content: NonEmpty


class StepsBlock(Strict):
    type: Literal["steps"]
    items: list[NonEmpty]


class QuestionsBlock(Strict):
    type: Literal["questions"]
    items: list[NonEmpty]


class ImageBlock(Strict):
    type: Literal["image"]
    src: NonEmpty
    alt: NonEmpty
    caption: str | None = None

    @field_validator("src")
    @classmethod
    def _inside_assets(cls, src: str) -> str:
        if not is_safe_asset_path(src):
            raise PydanticCustomError(
                "asset_path",
                "Image src '{src}' must be a relative path inside workbooks/assets/",
                {"src": src},
            )
        return src


class NoteBlock(Strict):
    type: Literal["note"]
    variant: Literal["info", "warning"] = "info"
    content: NonEmpty


Block = Annotated[
    TextBlock | TableBlock | SnippetBlock | StepsBlock | QuestionsBlock | ImageBlock | NoteBlock,
    Field(discriminator="type"),
]


def is_safe_asset_path(src: str) -> bool:
    """True if ``src`` (relative to workbooks/) stays inside workbooks/assets/."""
    if not src or "\\" in src or "%" in src or ":" in src or src.startswith("/"):
        return False
    parts = src.split("/")
    if ".." in parts:
        return False
    normalized = posixpath.normpath(src)
    return normalized.startswith("assets/") and normalized != "assets"


# --------------------------------------------------------------------------- structure


class Trainer(Strict):
    expectations: list[NonEmpty] = []
    notes: str | None = None


FIXED_CONTENT_FIELDS = ("requirement", "snippet", "steps", "guiding_questions")


class Task(Strict):
    id: Id
    number: str | None = None
    title: NonEmpty
    level: NonEmpty
    duration: Duration | None = None
    requirement: str | None = None
    snippet: str | None = None
    steps: list[NonEmpty] | None = None
    guiding_questions: list[NonEmpty] | None = None
    blocks: list[Block] | None = None
    hints: str | None = None
    answers: list[Answer] = []
    bonus: str | None = None
    trainer: Trainer | None = None

    @model_validator(mode="after")
    def _blocks_exclusive(self) -> Task:
        if self.blocks is not None:
            used = [name for name in FIXED_CONTENT_FIELDS if getattr(self, name) is not None]
            if used:
                raise PydanticCustomError(
                    "blocks_exclusive",
                    "'blocks' cannot be combined with {fields}",
                    {"fields": ", ".join(used)},
                )
        return self

    @field_validator("answers")
    @classmethod
    def _unique_answer_ids(cls, answers: list[Answer]) -> list[Answer]:
        _ensure_unique([a.id for a in answers], "answer")
        return answers


class Module(Strict):
    code: NonEmpty
    title: NonEmpty
    objective: str | None = None
    tasks: list[Task] = []


class Day(Strict):
    id: Id
    title: NonEmpty
    subtitle: str | None = None
    nav_label: str | None = None
    nav_tag: str | None = None
    optional: bool = False
    modules: list[Module] = []


class Catalog(Strict):
    schema_version: Literal[1]
    workbook: WorkbookMeta
    levels: dict[str, Level] = Field(min_length=1)
    days: list[Day]


# --------------------------------------------------------------------------- cross checks


def _ensure_unique(ids: list[str], kind: str) -> None:
    seen: set[str] = set()
    for item in ids:
        if item in seen:
            raise PydanticCustomError(
                "duplicate_id", "Duplicate {kind} id '{id}'", {"kind": kind, "id": item}
            )
        seen.add(item)


class ReferenceIssue(BaseModel):
    """A workbook-wide rule violation with the exact location of the offending value."""

    loc: tuple[str | int, ...]
    type: str
    message: str
    ctx: dict[str, str] = {}


def check_references(catalog: Catalog) -> list[ReferenceIssue]:
    """Rules that need the whole workbook: unique ids/codes and level references."""
    issues: list[ReferenceIssue] = []
    day_ids: set[str] = set()
    task_ids: set[str] = set()
    module_codes: set[str] = set()

    for d, day in enumerate(catalog.days):
        if day.id in day_ids:
            issues.append(_dup(("days", d, "id"), "day", day.id))
        day_ids.add(day.id)
        for m, module in enumerate(day.modules):
            base = ("days", d, "modules", m)
            if module.code in module_codes:
                issues.append(_dup((*base, "code"), "module code", module.code))
            module_codes.add(module.code)
            for t, task in enumerate(module.tasks):
                tloc = (*base, "tasks", t)
                if task.id in task_ids:
                    issues.append(_dup((*tloc, "id"), "task", task.id))
                task_ids.add(task.id)
                if task.level not in catalog.levels:
                    defined = ", ".join(catalog.levels)
                    issues.append(
                        ReferenceIssue(
                            loc=(*tloc, "level"),
                            type="unknown_level",
                            message=f"Unknown level '{task.level}' (defined: {defined})",
                            ctx={"level": task.level, "defined": defined},
                        )
                    )
    return issues


def _dup(loc: tuple[str | int, ...], kind: str, value: str) -> ReferenceIssue:
    return ReferenceIssue(
        loc=loc,
        type="duplicate_id",
        message=f"Duplicate {kind} id '{value}'",
        ctx={"kind": kind, "id": value},
    )
