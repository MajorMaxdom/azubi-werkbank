"""Backend of the form editor: load catalog files as plain data, validate,
protect ids that already have saved answers, and write YAML back while keeping
comments and formatting of untouched parts. Also stores uploaded images for
image blocks under ``workbooks/assets/<workbook-id>/``."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from app.files import atomic_write, locked
from app.loader import CatalogError, to_plain
from app.models.catalog import Catalog, ImageBlock, check_references
from app.models.progress import Progress

BACKUP_DIR = "_backups"
BACKUPS_KEPT = 10
BLOCK_TYPES = {"text", "table", "snippet", "steps", "questions", "image", "note"}
REQUIRED_TOP_LEVEL = {"schema_version", "workbook", "levels", "days"}

DEFAULT_LEVELS = {
    "understand": {"label": "Verstehen", "color": "blue",
                   "description": "Lesen, recherchieren und in eigenen Worten erklären."},
    "observe": {"label": "Beobachten", "color": "ochre",
                "description": "Mit vorgegebenem Klickpfad am System nachschauen, nichts ändern."},
    "apply": {"label": "Anwenden", "color": "green",
              "description": "Eine klar umrissene Sache selbst tun."},
}  # fmt: skip


class EditorConflict(Exception):
    """The file changed on disk since the editor loaded it."""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 100
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def read_document(path: Path) -> tuple[Any, str]:
    """Round-trip document (YAML) or plain data (JSON) and the file text."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text), text
    return _yaml().load(text), text


def load_for_editor(path: Path) -> tuple[dict, str]:
    """Plain data of a catalog file and the hash of its current text."""
    doc, text = read_document(path)
    return to_plain(doc), text_hash(text)


# --------------------------------------------------------------------------- cleaning & validation


def clean(value: Any, *, top: bool = True) -> Any:
    """Drop empty optional values (None, "", [], {}) sent by the form editor."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = clean(item, top=False)
            if top and key in REQUIRED_TOP_LEVEL:
                out[key] = item
            elif item is None or item == "" or item == [] or item == {}:
                continue
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [clean(v, top=False) for v in value]
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    return value


def validate(data: dict, name: str = "editor") -> tuple[Catalog | None, list[CatalogError]]:
    try:
        catalog = Catalog.model_validate(data)
    except ValidationError as exc:
        errors = []
        for err in exc.errors(include_url=False):
            ctx = {k: str(v) for k, v in (err.get("ctx") or {}).items()}
            errors.append(CatalogError(name, editor_loc(tuple(err["loc"])), err["type"],
                                       err["msg"], None, ctx))  # fmt: skip
        return None, errors
    errors = [
        CatalogError(name, issue.loc, issue.type, issue.message, None, dict(issue.ctx))
        for issue in check_references(catalog)
    ]
    return (catalog if not errors else None), errors


def editor_loc(loc: tuple) -> tuple:
    """Drop the union tag pydantic inserts after a block index (blocks.0.image.alt)."""
    out: list = []
    for part in loc:
        if (
            isinstance(part, str)
            and part in BLOCK_TYPES
            and len(out) >= 2
            and isinstance(out[-1], int)
            and out[-2] == "blocks"
        ):
            continue
        out.append(part)
    return tuple(out)


# --------------------------------------------------------------------------- id protection


def locked_ids(progress_root: Path, workbook_id: str) -> dict[str, set[str]]:
    """Task ids (and answer ids per task) that already have saved data."""
    locked_map: dict[str, set[str]] = {}
    directory = progress_root / workbook_id
    if not directory.is_dir():
        return locked_map
    for path in directory.glob("*.json"):
        try:
            progress = Progress.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            continue
        for task_id, tp in progress.tasks.items():
            used = (
                tp.done
                or tp.review is not None
                or any(v not in ("", []) for v in tp.answers.values())
            )
            if not used:
                continue
            answers = locked_map.setdefault(task_id, set())
            answers.update(aid for aid, v in tp.answers.items() if v not in ("", []))
    return locked_map


def missing_locked(catalog: Catalog, locked_map: dict[str, set[str]]) -> dict[str, list[str]]:
    """Locked tasks/answers that the new catalog no longer contains.

    Returns ``{task_id: []}`` for a removed task and ``{task_id: [answer ids]}``
    for removed answer fields of a task that still exists.
    """
    tasks = {t.id: t for d in catalog.days for m in d.modules for t in m.tasks}
    missing: dict[str, list[str]] = {}
    for task_id, answer_ids in sorted(locked_map.items()):
        task = tasks.get(task_id)
        if task is None:
            missing[task_id] = []
            continue
        present = {a.id for a in task.answers}
        gone = sorted(answer_ids - present)
        if gone:
            missing[task_id] = gone
    return missing


# --------------------------------------------------------------------------- writing


def prepare(value: Any) -> Any:
    """Plain data -> YAML-friendly data (multi-line strings as literal blocks)."""
    if isinstance(value, dict):
        return CommentedMap((k, prepare(v)) for k, v in value.items())
    if isinstance(value, list):
        return CommentedSeq(prepare(v) for v in value)
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value if value.endswith("\n") else value + "\n")
    return value


def _same_text(old: Any, new: str) -> bool:
    return isinstance(old, str) and old.rstrip("\n") == new.rstrip("\n")


def merge(old: Any, new: Any) -> Any:
    """Apply ``new`` (plain data) onto the round-trip node ``old`` in place where
    possible, so comments and styles of unchanged parts survive."""
    if isinstance(new, dict):
        if not isinstance(old, CommentedMap):
            return prepare(new)
        for key in [k for k in old if k not in new]:
            del old[key]
        for key, value in new.items():
            old[key] = merge(old[key], value) if key in old else prepare(value)
        return old
    if isinstance(new, list):
        if not isinstance(old, CommentedSeq):
            return prepare(new)
        id_key = _identity_key(new)
        if id_key:
            by_id = {
                item[id_key]: item
                for item in old
                if isinstance(item, CommentedMap) and id_key in item
            }
            items = [merge(by_id[n[id_key]], n) if n[id_key] in by_id else prepare(n) for n in new]
        else:
            items = [merge(old[i], n) if i < len(old) else prepare(n) for i, n in enumerate(new)]
        reordered = [id(a) for a in items] != [id(b) for b in old[: len(items)]]
        del old[:]
        old.extend(items)
        if reordered:
            old.ca.items.clear()  # comments are attached by position
        return old
    if isinstance(new, str):
        if _same_text(old, new):
            return old
        return prepare(new)
    return new


def _identity_key(items: list) -> str | None:
    for key in ("id", "code"):
        if items and all(isinstance(i, dict) and isinstance(i.get(key), str) for i in items):
            return key
    return None


def render_document(path: Path, data: dict) -> str:
    if path.suffix.lower() == ".json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        doc, _ = read_document(path)
        doc = merge(doc, data) if isinstance(doc, CommentedMap) else prepare(data)
    else:
        doc = prepare(data)
    out = io.StringIO()
    _yaml().dump(doc, out)
    return out.getvalue()


def save(path: Path, data: dict, base_hash: str | None, header: str = "") -> str:
    """Write ``data`` to ``path`` (backup first). Returns the new text hash.

    Raises ``EditorConflict`` when the file changed since ``base_hash``.
    """
    with locked(path):
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if base_hash is not None and text_hash(current) != base_hash:
                raise EditorConflict(path.name)
            backup(path)
        elif base_hash is not None:
            raise EditorConflict(path.name)
        text = render_document(path, data)
        if header and not path.exists() and path.suffix.lower() != ".json":
            text = header + text
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o664
        atomic_write(path, text, mode=mode)
        return text_hash(text)


def backup(path: Path) -> Path:
    directory = path.parent / BACKUP_DIR
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, target)
    old = sorted(directory.glob(f"{path.stem}.*{path.suffix}"))
    for stale in old[:-BACKUPS_KEPT]:
        stale.unlink(missing_ok=True)
    return target


# --------------------------------------------------------------------------- new workbooks


def new_workbook_data(workbook_id: str, title: str, source: dict | None = None) -> dict:
    if source is not None:
        data = copy.deepcopy(source)
        data["workbook"]["id"] = workbook_id
        data["workbook"]["title"] = title
        data["workbook"]["version"] = "1.0.0"
        return data
    return {
        "schema_version": 1,
        "workbook": {"id": workbook_id, "version": "1.0.0", "title": title},
        "levels": copy.deepcopy(DEFAULT_LEVELS),
        "days": [
            {
                "id": "day-1",
                "title": "Tag 1",
                "modules": [
                    {
                        "code": "MOD-01",
                        "title": "Erstes Modul",
                        "tasks": [
                            {
                                "id": "mod01-task-1",
                                "title": "Erste Aufgabe",
                                "level": "understand",
                                "duration": "30m",
                                "requirement": "Beschreibe hier die Aufgabe.",
                                "answers": [{"id": "answer", "label": "Deine Antwort:"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- image assets

MAX_ASSET_BYTES = 5 * 1024 * 1024
ASSET_FILE_MODE = 0o664
ASSET_DIR_MODE = 0o775
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


class AssetTooLarge(Exception):
    """The uploaded image exceeds ``MAX_ASSET_BYTES``."""


def detect_image_type(head: bytes) -> str | None:
    """File extension (without dot) for PNG, JPEG, GIF or WebP data, else None.

    Only the file signature counts; names and declared content types are ignored.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def asset_basename(filename: str | None) -> str:
    """Safe file stem from an uploaded name: lowercase, ä→ae …, only ``[a-z0-9-]``."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    stem = stem.lower().translate(_TRANSLIT)
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:60].strip("-")
    return stem or "bild"


def asset_directory(workbooks_dir: Path, workbook_id: str) -> Path:
    return workbooks_dir / "assets" / workbook_id


def store_asset(directory: Path, filename: str | None, chunks: Iterable[bytes]) -> str:
    """Write an uploaded image into ``directory`` and return its new file name.

    The data is streamed into a temp file (at most ``MAX_ASSET_BYTES``), its
    signature decides the extension, and it is hard-linked under a free name, so
    an existing file is never overwritten (``-2``, ``-3`` … on collision).
    Raises ``AssetTooLarge`` or ``ValueError`` (not a PNG/JPEG/GIF/WebP image).
    """
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, ASSET_DIR_MODE)
    fd, tmp = tempfile.mkstemp(prefix=".upload.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, ASSET_FILE_MODE)
        size = 0
        head = b""
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    raise AssetTooLarge
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        ext = detect_image_type(head)
        if ext is None:
            raise ValueError("unsupported image type")
        base = asset_basename(filename)
        n = 1
        while True:
            name = f"{base}.{ext}" if n == 1 else f"{base}-{n}.{ext}"
            try:
                os.link(tmp, directory / name)
            except FileExistsError:
                n += 1
                continue
            return name
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def list_assets(directory: Path) -> list[dict[str, Any]]:
    """Images in ``directory`` as ``{name, size}`` (sorted by name)."""
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.iterdir()):
        if (
            path.name.startswith(".")
            or path.suffix.lower() not in ASSET_EXTENSIONS
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        out.append({"name": path.name, "size": path.stat().st_size})
    return out


def is_asset_name(name: str) -> bool:
    return bool(_ASSET_NAME_RE.fullmatch(name)) and ".." not in name


def asset_usage(views: Iterable[Any]) -> dict[str, list[str]]:
    """``src`` -> where it is used ("<workbook title> · <task number> <task title>").

    Image blocks count, and so does any mention of the path in task text (e.g. a
    Markdown image), so an image is only "unused" if nothing refers to it.
    """
    usage: dict[str, list[str]] = {}
    for view in views:
        for tv in view.tasks_by_id.values():
            label = f"{view.meta.title} · {tv.number} {tv.task.title}"
            paths = {
                posixpath.normpath(b.src) for b in tv.task.blocks or [] if isinstance(b, ImageBlock)
            }
            text = tv.task.model_dump_json()
            for src in paths:
                usage.setdefault(src, []).append(label)
            for match in set(re.findall(r"assets/[A-Za-z0-9._/-]+", text)):
                src = posixpath.normpath(match)
                if src not in paths:
                    usage.setdefault(src, []).append(label)
    return usage
