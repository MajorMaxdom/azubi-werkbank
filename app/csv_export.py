"""CSV downloads for German Excel: UTF-8 with BOM, ``;`` delimiter, CRLF line ends.

Cells that a spreadsheet would treat as a formula are prefixed with ``'``
(CSV/formula injection protection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence

from fastapi.responses import Response

from app.i18n import Translator

COLUMNS = (
    "apprentice",
    "username",
    "workbook",
    "module",
    "number",
    "task",
    "status",
    "done_at",
    "reviewed_by",
    "reviewed_at",
    "comment",
    "supervisor",
)

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_START):
        return "'" + text
    return text


def build_csv(t: Translator, rows: Iterable[Sequence[object]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow([t(f"csv.{column}") for column in COLUMNS])
    for row in rows:
        writer.writerow([safe_cell(value) for value in row])
    return ("﻿" + out.getvalue()).encode("utf-8")


def csv_response(content: bytes, filename: str) -> Response:
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
