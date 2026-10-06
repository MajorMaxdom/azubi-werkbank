from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent

MINIMAL: dict[str, Any] = {
    "schema_version": 1,
    "workbook": {"id": "demo", "version": "1.0.0", "title": "Demo"},
    "levels": {"understand": {"label": "Verstehen", "color": "blue"}},
    "days": [
        {
            "id": "day-1",
            "title": "Tag eins",
            "modules": [
                {
                    "code": "M-01",
                    "title": "Modul",
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Aufgabe",
                            "level": "understand",
                            "duration": "45m",
                            "requirement": "Tu etwas.",
                            "answers": [{"id": "a1", "label": "Antwort"}],
                            "trainer": {
                                "expectations": ["GEHEIM-ERWARTUNG"],
                                "notes": "GEHEIM-NOTIZ",
                            },
                        }
                    ],
                }
            ],
        }
    ],
}


@pytest.fixture
def catalog_data() -> dict[str, Any]:
    """A fresh, valid minimal catalog as plain data (safe to mutate)."""
    return copy.deepcopy(MINIMAL)


def task_of(data: dict[str, Any], day: int = 0, module: int = 0, task: int = 0) -> dict[str, Any]:
    return data["days"][day]["modules"][module]["tasks"][task]
