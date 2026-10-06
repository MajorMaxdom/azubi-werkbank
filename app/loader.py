"""Parse and validate catalog files.

Phase 0.1.0 provides file/directory loading for the CLI. The registry and the
file watcher are added in 0.2.0 on top of these functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError

from app.models.catalog import Catalog, check_references

CATALOG_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class CatalogError:
    file: str
    loc: tuple[str | int, ...]
    type: str
    message: str
    line: int | None = None

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
            result.errors.append(
                CatalogError(name, loc, err["type"], err["msg"], find_line(raw, loc))
            )
        return result

    for issue in check_references(catalog):
        result.errors.append(
            CatalogError(name, issue.loc, issue.type, issue.message, find_line(raw, issue.loc))
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
