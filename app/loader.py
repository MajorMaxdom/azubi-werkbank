"""Parse and validate catalog files; in-memory registry with hot reload.

Each catalog file has a registry entry holding its last valid catalog and its
current errors. A broken file keeps serving its last valid version. Workbook
ids must be unique across files; on conflict the file that sorts first by
name wins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError
from watchfiles import Change, awatch

from app.i18n import Translator
from app.models.catalog import Catalog, check_references
from app.renderer import WorkbookView, build_view

log = logging.getLogger(__name__)

CATALOG_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class CatalogError:
    file: str
    loc: tuple[str | int, ...]
    type: str
    message: str
    line: int | None = None
    ctx: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Key path in dotted form, e.g. ``days[0].modules[1].tasks[2].id``."""
        out = ""
        for part in self.loc:
            if isinstance(part, int):
                out += f"[{part}]"
            else:
                out += f".{part}" if out else str(part)
        return out

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        path = f" {self.path}:" if self.loc else ""
        return f"{where}:{path} {self.message}"


@dataclass
class LoadResult:
    file: Path
    catalog: Catalog | None = None
    errors: list[CatalogError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.catalog is not None and not self.errors


def is_catalog_file(path: Path) -> bool:
    return path.suffix.lower() in CATALOG_SUFFIXES and not path.name.startswith("_")


def load_file(path: Path) -> LoadResult:
    """Parse and validate a single catalog file. Never raises for content errors."""
    result = LoadResult(file=path)
    name = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        result.errors.append(CatalogError(name, (), "read_error", f"Cannot read file: {exc}"))
        return result

    raw: Any
    if path.suffix.lower() == ".json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            result.errors.append(CatalogError(name, (), "parse_error", exc.msg, exc.lineno))
            return result
    else:
        try:
            raw = YAML(typ="rt").load(text)
        except MarkedYAMLError as exc:
            line = exc.problem_mark.line + 1 if exc.problem_mark else None
            message = " ".join(str(p) for p in (exc.context, exc.problem) if p)
            result.errors.append(CatalogError(name, (), "parse_error", message, line))
            return result

    if not isinstance(raw, dict):
        result.errors.append(
            CatalogError(name, (), "parse_error", "Top level must be a mapping", 1)
        )
        return result

    try:
        catalog = Catalog.model_validate(to_plain(raw))
    except ValidationError as exc:
        for err in exc.errors(include_url=False):
            loc = tuple(err["loc"])
            ctx = {k: str(v) for k, v in (err.get("ctx") or {}).items()}
            result.errors.append(
                CatalogError(name, loc, err["type"], err["msg"], find_line(raw, loc), ctx)
            )
        return result

    for issue in check_references(catalog):
        line = find_line(raw, issue.loc)
        result.errors.append(
            CatalogError(name, issue.loc, issue.type, issue.message, line, dict(issue.ctx))
        )
    if not result.errors:
        result.catalog = catalog
    return result


def load_directory(directory: Path) -> tuple[dict[str, Catalog], list[LoadResult]]:
    """Load every catalog in ``directory``.

    Returns the valid catalogs by workbook id and the per-file results. A workbook
    id used by more than one file is an error for every file after the first
    (files are processed in name order so the outcome is deterministic).
    """
    catalogs: dict[str, Catalog] = {}
    owners: dict[str, Path] = {}
    results: list[LoadResult] = []
    for path in sorted(p for p in directory.iterdir() if p.is_file() and is_catalog_file(p)):
        result = load_file(path)
        if result.ok:
            assert result.catalog is not None
            wid = result.catalog.workbook.id
            if wid in owners:
                result.errors.append(
                    CatalogError(
                        str(path),
                        ("workbook", "id"),
                        "duplicate_workbook",
                        f"Workbook id '{wid}' is already used by {owners[wid].name}",
                        ctx={"id": wid, "file": owners[wid].name},
                    )
                )
                result.catalog = None
            else:
                owners[wid] = path
                catalogs[wid] = result.catalog
        results.append(result)
    return catalogs, results


def to_plain(value: Any) -> Any:
    """Convert ruamel round-trip containers and scalar subclasses to plain Python."""
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def find_line(root: Any, loc: tuple[str | int, ...]) -> int | None:
    """Best-effort 1-based line number for a pydantic error location (YAML only)."""
    node = root
    line: int | None = None
    for part in loc:
        if isinstance(node, CommentedMap) and part in node:
            line = node.lc.key(part)[0] + 1
            node = node[part]
        elif isinstance(node, CommentedSeq) and isinstance(part, int) and part < len(node):
            line = node.lc.item(part)[0] + 1
            node = node[part]
        # Other parts (e.g. union tags like "image") do not exist in the data: skip.
    return line


# --------------------------------------------------------------------------- registry


@dataclass
class Entry:
    path: Path
    catalog: Catalog | None = None  # last valid version
    errors: list[CatalogError] = field(default_factory=list)
    loaded_at: datetime | None = None  # when `catalog` was last replaced
    checked_at: datetime | None = None  # when the file was last parsed
    conflict: CatalogError | None = None  # duplicate workbook id across files

    @property
    def all_errors(self) -> list[CatalogError]:
        return [*self.errors, *([self.conflict] if self.conflict else [])]

    @property
    def workbook_id(self) -> str | None:
        return self.catalog.workbook.id if self.catalog else None


class Registry:
    def __init__(self, directory: Path, translator: Translator | None = None) -> None:
        self.directory = directory
        self._translator = translator
        self._entries: dict[Path, Entry] = {}
        self._by_id: dict[str, Entry] = {}
        self._views: dict[str, WorkbookView] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[set[str]], None]] = []

    # ------------------------------------------------------------------ queries

    def entries(self) -> list[Entry]:
        with self._lock:
            return [self._entries[p] for p in sorted(self._entries)]

    def catalogs(self) -> dict[str, Catalog]:
        with self._lock:
            return {wid: e.catalog for wid, e in sorted(self._by_id.items()) if e.catalog}

    def entry_for(self, workbook_id: str) -> Entry | None:
        """The file entry currently serving ``workbook_id``."""
        with self._lock:
            return self._by_id.get(workbook_id)

    def get(self, workbook_id: str) -> Catalog | None:
        with self._lock:
            entry = self._by_id.get(workbook_id)
            return entry.catalog if entry else None

    def view(self, workbook_id: str) -> WorkbookView | None:
        with self._lock:
            catalog = self.get(workbook_id)
            if catalog is None:
                return None
            view = self._views.get(workbook_id)
            if view is None or view.catalog is not catalog:
                view = build_view(catalog, self._translator)
                self._views[workbook_id] = view
            return view

    # ------------------------------------------------------------------ updates

    def on_change(self, listener: Callable[[set[str]], None]) -> None:
        """Register a callback receiving the set of changed workbook ids."""
        self._listeners.append(listener)

    def load_all(self) -> None:
        with self._lock:
            before = self._snapshot()
            self._entries.clear()
            if self.directory.is_dir():
                for path in self.directory.iterdir():
                    if path.is_file() and is_catalog_file(path):
                        self._reload(path)
            self._reindex()
            changed = self._diff(before)
        self._notify(changed)

    def apply_changes(self, paths: Iterable[Path]) -> set[str]:
        """Reload or remove the given files. Returns the changed workbook ids."""
        with self._lock:
            before = self._snapshot()
            for path in paths:
                if not is_catalog_file(path) or path.parent.resolve() != self.directory.resolve():
                    continue
                key = self._key(path)
                if path.is_file():
                    self._reload(key)
                elif key in self._entries:
                    log.info("catalog.removed file=%s", key.name)
                    del self._entries[key]
            self._reindex()
            changed = self._diff(before)
        self._notify(changed)
        return changed

    def _key(self, path: Path) -> Path:
        return self.directory / path.name

    def _reload(self, path: Path) -> None:
        key = self._key(path)
        entry = self._entries.setdefault(key, Entry(path=key))
        result = load_file(key)
        now = datetime.now(UTC)
        entry.checked_at = now
        entry.errors = result.errors
        if result.catalog is not None:
            entry.catalog = result.catalog
            entry.loaded_at = now
            log.info("catalog.loaded file=%s id=%s", key.name, result.catalog.workbook.id)
        else:
            log.warning(
                "catalog.invalid file=%s errors=%d kept_previous=%s",
                key.name,
                len(result.errors),
                entry.catalog is not None,
            )

    def _reindex(self) -> None:
        by_id: dict[str, Entry] = {}
        for path in sorted(self._entries):
            entry = self._entries[path]
            entry.conflict = None
            wid = entry.workbook_id
            if wid is None:
                continue
            if wid in by_id:
                entry.conflict = CatalogError(
                    str(path),
                    ("workbook", "id"),
                    "duplicate_workbook",
                    f"Workbook id '{wid}' is already used by {by_id[wid].path.name}",
                    ctx={"id": wid, "file": by_id[wid].path.name},
                )
                continue
            by_id[wid] = entry
        self._by_id = by_id
        self._views = {k: v for k, v in self._views.items() if k in by_id}

    def _snapshot(self) -> dict[str, Catalog]:
        return {wid: e.catalog for wid, e in self._by_id.items() if e.catalog}

    def _diff(self, before: dict[str, Catalog]) -> set[str]:
        after = self._snapshot()
        return {
            wid for wid in before.keys() | after.keys() if before.get(wid) is not after.get(wid)
        }

    def _notify(self, changed: set[str]) -> None:
        if not changed:
            return
        for listener in self._listeners:
            try:
                listener(changed)
            except Exception:
                log.exception("registry listener failed")

    # ------------------------------------------------------------------ watching

    async def watch(self, stop_event: asyncio.Event | None = None, debounce: int = 400) -> None:
        """Watch the directory and apply changes until ``stop_event`` is set."""
        self.directory.mkdir(parents=True, exist_ok=True)
        changes: AsyncIterator[set[tuple[Change, str]]] = awatch(
            self.directory, stop_event=stop_event, debounce=debounce, recursive=False
        )
        async for batch in changes:
            paths = {Path(p) for _, p in batch}
            await asyncio.to_thread(self.apply_changes, paths)
