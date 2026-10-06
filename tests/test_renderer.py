from __future__ import annotations

import pytest

from app.i18n import get_translator
from app.models.catalog import Catalog
from app.renderer import (
    build_view,
    day_load_text,
    format_hours,
    parse_duration,
    render_markdown,
    render_static,
    task_hash,
)
from tests.conftest import task_of


@pytest.mark.parametrize(
    ("value", "minutes"),
    [("45m", 45), ("1h", 60), ("1h30m", 90), ("2h5m", 125), ("0m", 0), (None, 0), ("", 0)],
)
def test_parse_duration(value, minutes):
    assert parse_duration(value) == minutes


@pytest.mark.parametrize("value", ["1.5h", "30", "m30", "1h\n"])
def test_parse_duration_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    ("minutes", "text"),
    # Values from the reference workbook day banners.
    [(150, "2,5"), (165, "2,8"), (180, "3"), (195, "3,2"), (60, "1"), (30, "0,5"), (20, "0,3")],
)
def test_format_hours(minutes, text):
    assert format_hours(minutes) == text


def test_day_load_text():
    t = get_translator()
    assert day_load_text(3, 150, t) == "3 Aufgaben · ~2,5 h"
    assert day_load_text(1, 60, t) == "1 Aufgabe · ~1 h"
    assert day_load_text(2, 0, t) == "2 Aufgaben"


def _two_day_catalog(data):
    task = task_of(data)
    data["days"][0]["modules"][0]["tasks"].append({**task, "id": "t2", "duration": "1h30m"})
    data["days"].append(
        {
            "id": "final",
            "title": "Abschluss",
            "optional": True,
            "nav_tag": "OPT",
            "modules": [{"code": "P-01", "title": "P", "tasks": [{**task, "id": "t3"}]}],
        }
    )
    data["days"].append(
        {
            "id": "extra",
            "title": "Extra",
            "modules": [
                {"code": "X-01", "title": "X", "tasks": [{**task, "id": "t4", "number": "9.9"}]}
            ],
        }
    )
    return Catalog.model_validate(data)


def test_numbering_and_day_load(catalog_data):
    view = build_view(_two_day_catalog(catalog_data))
    numbers = {tid: tv.number for tid, tv in view.tasks_by_id.items()}
    assert numbers == {"t1": "1.1", "t2": "1.2", "t3": "OPT.1", "t4": "9.9"}
    day1, final, extra = view.days
    assert day1.load == "2 Aufgaben · ~2,2 h"  # 135 min; half-even like the reference
    assert day1.label == "Tag 1" and day1.nav_tag == "1" and day1.nav_label == "Tag eins"
    assert final.label == "Optional" and final.nav_tag == "OPT"
    assert extra.label == "Tag 3" and extra.nav_tag == "3"
    assert view.task_count == 4


def test_task_numbers_continue_across_modules(catalog_data):
    module = catalog_data["days"][0]["modules"][0]
    second = {"code": "M-02", "title": "Zwei", "tasks": [{**module["tasks"][0], "id": "t9"}]}
    catalog_data["days"][0]["modules"].append(second)
    view = build_view(Catalog.model_validate(catalog_data))
    assert view.tasks_by_id["t9"].number == "1.2"


def test_task_hash_ignores_trainer_content(catalog_data):
    base = Catalog.model_validate(catalog_data).days[0].modules[0].tasks[0]
    task_of(catalog_data)["trainer"] = {"expectations": ["anders"], "notes": "neu"}
    changed_trainer = Catalog.model_validate(catalog_data).days[0].modules[0].tasks[0]
    task_of(catalog_data)["requirement"] = "Tu etwas anderes."
    changed_content = Catalog.model_validate(catalog_data).days[0].modules[0].tasks[0]
    assert task_hash(base) == task_hash(changed_trainer)
    assert task_hash(base) != task_hash(changed_content)
    assert len(task_hash(base)) == 64


def test_markdown_escapes_raw_html():
    html = render_markdown("**fett** <script>alert(1)</script> <b>x</b>")
    assert "<strong>fett</strong>" in html
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_blocks_javascript_links():
    html = render_markdown("[klick](javascript:alert(1))")
    assert 'href="javascript' not in html


def test_catalog_plain_text_is_escaped(catalog_data):
    task_of(catalog_data)["title"] = "<img src=x onerror=alert(1)>"
    task_of(catalog_data)["snippet"] = "<b>raw</b>"
    html = render_static(Catalog.model_validate(catalog_data))
    assert "<img src=x" not in html and "<b>raw</b>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_trainer_content_only_in_trainer_view(catalog_data):
    catalog = Catalog.model_validate(catalog_data)
    assert "GEHEIM-ERWARTUNG" in render_static(catalog, trainer=True)
    apprentice = render_static(catalog, trainer=False)
    assert "GEHEIM-ERWARTUNG" not in apprentice
    assert "GEHEIM-NOTIZ" not in apprentice
    assert 'class="trainer"' not in apprentice


def test_answer_types_render(catalog_data):
    task_of(catalog_data)["answers"] = [
        {"id": "txt", "type": "text", "label": "L1", "height": 150},
        {"id": "mono", "type": "text", "monospace": True},
        {"id": "sh", "type": "short", "label": "L2", "placeholder": "P"},
        {"id": "cl", "type": "checklist", "label": "L3", "items": ["A", "B"]},
        {"id": "ch", "type": "choice", "label": "L4", "options": ["X", "Y"]},
        {"id": "chm", "type": "choice", "multiple": True, "options": ["X", "Y"]},
        {"id": "dt", "type": "date", "label": "L5"},
    ]
    html = render_static(Catalog.model_validate(catalog_data))
    assert '<textarea id="a-t1-txt" rows="6"' in html
    assert '<textarea id="a-t1-mono" class="mono-input" rows="4"' in html
    assert '<input id="a-t1-sh" type="text" value="" placeholder="P">' in html
    assert html.count('type="checkbox" name="a-t1-cl"') == 2
    assert html.count('type="radio" name="a-t1-ch"') == 2
    assert html.count('type="checkbox" name="a-t1-chm"') == 2
    assert '<input id="a-t1-dt" type="date" value="">' in html


def test_static_output_is_self_contained(catalog_data):
    html = render_static(Catalog.model_validate(catalog_data))
    assert "/static/" not in html
    assert "data:font/woff2;base64," in html
    assert "<script" not in html
