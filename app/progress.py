"""Per-user progress files: ``progress/<workbook-id>/<username>.json``.

Writes are atomic (temp file + ``os.replace``) under a per-file lock; files are
created with mode 0600 and directories with 0700. Ids are validated before they
are used in a path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from app.files import atomic_write, ensure_private_dir, locked
from app.models.catalog import ID_PATTERN, Answer, Catalog, HeaderField, Task
from app.models.progress import AnswerValue, Progress

log = logging.getLogger(__name__)

_ID_RE = re.compile(ID_PATTERN)
MAX_ANSWER_CHARS = 20_000
MAX_REQUEST_BYTES = 1_000_000


class InvalidInput(ValueError):
    """Client data that does not fit the catalog (-> HTTP 422)."""


class CorruptProgress(RuntimeError):
    """A progress file exists but cannot be read; it is never overwritten."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class ProgressStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, workbook_id: str, username: str) -> Path:
        if not _ID_RE.fullmatch(workbook_id) or not _ID_RE.fullmatch(username):
            raise ValueError("invalid id in progress path")
        return self.root / workbook_id / f"{username}.json"

    def load(self, workbook_id: str, username: str) -> Progress:
        path = self.path(workbook_id, username)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Progress.model_validate(data)
        except FileNotFoundError:
            return Progress(workbook=workbook_id, user=username)
        except (ValueError, ValidationError) as exc:
            log.error("progress.invalid workbook=%s user=%s", workbook_id, username)
            raise CorruptProgress(str(path)) from exc

    def exists(self, workbook_id: str, username: str) -> bool:
        return self.path(workbook_id, username).is_file()

    def update(
        self, catalog: Catalog, username: str, change: Callable[[Progress], None]
    ) -> Progress:
        workbook_id = catalog.workbook.id
        path = self.path(workbook_id, username)
        ensure_private_dir(self.root)
        ensure_private_dir(path.parent)
        with locked(path):
            progress = self.load(workbook_id, username)
            change(progress)
            progress.workbook_version = catalog.workbook.version
            progress.updated_at = utcnow()
            atomic_write(path, progress.model_dump_json(indent=2) + "\n")
            return progress


# --------------------------------------------------------------------------- validation


def find_task(catalog: Catalog, task_id: str) -> Task | None:
    for day in catalog.days:
        for module in day.modules:
            for task in module.tasks:
                if task.id == task_id:
                    return task
    return None


def all_task_ids(catalog: Catalog) -> set[str]:
    return {t.id for d in catalog.days for m in d.modules for t in m.tasks}


def _check_text(value: object, answer_id: str) -> str:
    if not isinstance(value, str):
        raise InvalidInput(f"answer '{answer_id}' must be a string")
    if len(value) > MAX_ANSWER_CHARS:
        raise InvalidInput(f"answer '{answer_id}' is longer than {MAX_ANSWER_CHARS} characters")
    return value


def _check_date(value: object, field: str) -> str:
    text = _check_text(value, field)
    if text:
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise InvalidInput(f"'{field}' must be a date (YYYY-MM-DD)") from exc
    return text


def validate_answer(answer: Answer, value: object) -> AnswerValue:
    if answer.type in ("text", "short"):
        return _check_text(value, answer.id)
    if answer.type == "date":
        return _check_date(value, answer.id)
    choices = (answer.items if answer.type == "checklist" else answer.options) or []
    multi = answer.type == "checklist" or answer.multiple
    if multi:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidInput(f"answer '{answer.id}' must be a list of strings")
        unknown = [v for v in value if v not in choices]
        if unknown:
            raise InvalidInput(f"answer '{answer.id}' has unknown options")
        return [c for c in choices if c in value]  # catalog order, no duplicates
    if not isinstance(value, str) or (value and value not in choices):
        raise InvalidInput(f"answer '{answer.id}' must be one of the options")
    return value


def validate_answers(task: Task, answers: dict[str, object]) -> dict[str, AnswerValue]:
    by_id = {a.id: a for a in task.answers}
    clean: dict[str, AnswerValue] = {}
    for answer_id, value in answers.items():
        answer = by_id.get(answer_id)
        if answer is None:
            raise InvalidInput(f"unknown answer id '{answer_id}'")
        clean[answer_id] = validate_answer(answer, value)
    return clean


def validate_header(fields: list[HeaderField], values: dict[str, object]) -> dict[str, str]:
    by_id = {f.id: f for f in fields}
    clean: dict[str, str] = {}
    for field_id, value in values.items():
        field = by_id.get(field_id)
        if field is None:
            raise InvalidInput(f"unknown header field '{field_id}'")
        clean[field_id] = (
            _check_date(value, field_id) if field.type == "date" else _check_text(value, field_id)
        )
    return clean
