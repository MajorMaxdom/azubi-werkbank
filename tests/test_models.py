from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.catalog import Catalog, check_references, is_safe_asset_path
from tests.conftest import task_of


def errors_for(data: dict[str, Any]) -> list[tuple[tuple, str]]:
    """Validation errors as (loc, type); includes workbook-wide reference checks."""
    try:
        catalog = Catalog.model_validate(data)
    except ValidationError as exc:
        return [(tuple(e["loc"]), e["type"]) for e in exc.errors()]
    return [(i.loc, i.type) for i in check_references(catalog)]


def types(data: dict[str, Any]) -> set[str]:
    return {t for _, t in errors_for(data)}


def test_minimal_catalog_is_valid(catalog_data):
    assert errors_for(catalog_data) == []


# ------------------------------------------------------------------ ids


@pytest.mark.parametrize("bad", ["Upper", "-start", "with_underscore", "a" * 64, "", "ü", "a b"])
def test_invalid_ids_rejected_everywhere(catalog_data, bad):
    for setter in (
        lambda d: d["workbook"].__setitem__("id", bad),
        lambda d: d["days"][0].__setitem__("id", bad),
        lambda d: task_of(d).__setitem__("id", bad),
        lambda d: task_of(d)["answers"][0].__setitem__("id", bad),
        lambda d: d["workbook"].__setitem__("header_fields", [{"id": bad, "label": "X"}]),
    ):
        data = copy.deepcopy(catalog_data)
        setter(data)
        assert "string_pattern_mismatch" in types(data)


def test_id_regex_has_no_trailing_newline_loophole(catalog_data):
    catalog_data["workbook"]["id"] = "demo\n"
    assert "string_pattern_mismatch" in types(catalog_data)


def test_valid_id_edge_cases(catalog_data):
    catalog_data["workbook"]["id"] = "0" + "a" * 62
    assert errors_for(catalog_data) == []


def test_duplicate_day_ids(catalog_data):
    catalog_data["days"].append({**catalog_data["days"][0], "modules": []})
    assert errors_for(catalog_data) == [(("days", 1, "id"), "duplicate_id")]


def test_duplicate_task_ids_across_days(catalog_data):
    day2 = {
        "id": "day-2",
        "title": "Zwei",
        "modules": [{"code": "M-02", "title": "Zwei", "tasks": [dict(task_of(catalog_data))]}],
    }
    catalog_data["days"].append(day2)
    assert errors_for(catalog_data) == [
        (("days", 1, "modules", 0, "tasks", 0, "id"), "duplicate_id")
    ]


def test_duplicate_answer_ids(catalog_data):
    task_of(catalog_data)["answers"].append({"id": "a1"})
    assert ("days", 0, "modules", 0, "tasks", 0, "answers") in {
        loc for loc, t in errors_for(catalog_data) if t == "duplicate_id"
    }


def test_duplicate_header_field_ids(catalog_data):
    catalog_data["workbook"]["header_fields"] = [
        {"id": "x", "label": "A"},
        {"id": "x", "label": "B"},
    ]
    assert (("workbook", "header_fields"), "duplicate_id") in errors_for(catalog_data)


def test_duplicate_module_code(catalog_data):
    module = catalog_data["days"][0]["modules"][0]
    catalog_data["days"][0]["modules"].append({"code": module["code"], "title": "Noch eins"})
    assert errors_for(catalog_data) == [(("days", 0, "modules", 1, "code"), "duplicate_id")]


# ------------------------------------------------------------------ references & structure


def test_unknown_level(catalog_data):
    task_of(catalog_data)["level"] = "expert"
    assert errors_for(catalog_data) == [
        (("days", 0, "modules", 0, "tasks", 0, "level"), "unknown_level")
    ]


def test_unknown_key_is_error(catalog_data):
    task_of(catalog_data)["titel"] = "Tippfehler"
    assert "extra_forbidden" in types(catalog_data)


def test_missing_required_fields(catalog_data):
    del task_of(catalog_data)["title"]
    assert (("days", 0, "modules", 0, "tasks", 0, "title"), "missing") in errors_for(catalog_data)


def test_schema_version_must_be_1(catalog_data):
    catalog_data["schema_version"] = 2
    assert "literal_error" in types(catalog_data)


def test_level_color_must_be_known(catalog_data):
    catalog_data["levels"]["understand"]["color"] = "purple"
    assert "literal_error" in types(catalog_data)


@pytest.mark.parametrize("field", ["requirement", "snippet", "steps", "guiding_questions"])
def test_blocks_exclusive_with_fixed_fields(catalog_data, field):
    task = task_of(catalog_data)
    task.pop("requirement")
    task[field] = ["x"] if field in ("steps", "guiding_questions") else "x"
    task["blocks"] = [{"type": "text", "content": "Hallo"}]
    assert "blocks_exclusive" in types(catalog_data)


def test_blocks_alone_are_valid(catalog_data):
    task = task_of(catalog_data)
    task.pop("requirement")
    task["blocks"] = [
        {"type": "text", "content": "Hallo"},
        {"type": "table", "columns": ["A", "B"], "rows": [["1", "2"]]},
        {"type": "snippet", "content": "$ ls"},
        {"type": "steps", "items": ["eins"]},
        {"type": "questions", "items": ["warum?"]},
        {"type": "image", "src": "assets/demo/a.png", "alt": "Bild"},
        {"type": "note", "variant": "warning", "content": "Achtung"},
    ]
    assert errors_for(catalog_data) == []


def test_unknown_block_type(catalog_data):
    task = task_of(catalog_data)
    task.pop("requirement")
    task["blocks"] = [{"type": "video", "src": "x"}]
    assert "union_tag_invalid" in types(catalog_data)


def test_choice_needs_options(catalog_data):
    task_of(catalog_data)["answers"] = [{"id": "c", "type": "choice"}]
    assert "choice_options" in types(catalog_data)
    task_of(catalog_data)["answers"] = [{"id": "c", "type": "choice", "options": []}]
    assert "choice_options" in types(catalog_data)
    task_of(catalog_data)["answers"] = [{"id": "c", "type": "choice", "options": ["a", "b"]}]
    assert errors_for(catalog_data) == []


def test_checklist_needs_items(catalog_data):
    task_of(catalog_data)["answers"] = [{"id": "c", "type": "checklist"}]
    assert "checklist_items" in types(catalog_data)
    task_of(catalog_data)["answers"] = [{"id": "c", "type": "checklist", "items": ["a"]}]
    assert errors_for(catalog_data) == []


def test_image_needs_alt(catalog_data):
    task = task_of(catalog_data)
    task.pop("requirement")
    task["blocks"] = [{"type": "image", "src": "assets/a.png"}]
    assert ("days", 0, "modules", 0, "tasks", 0, "blocks", 0, "image", "alt") in {
        loc for loc, _ in errors_for(catalog_data)
    }
    task["blocks"] = [{"type": "image", "src": "assets/a.png", "alt": ""}]
    assert "string_too_short" in types(catalog_data)


# ------------------------------------------------------------------ formats


@pytest.mark.parametrize("value", ["45m", "1h", "1h30m", "2h0m", "0m"])
def test_valid_durations(catalog_data, value):
    task_of(catalog_data)["duration"] = value
    assert errors_for(catalog_data) == []


@pytest.mark.parametrize("value", ["", "45", "30m1h", "1.5h", "45 m", "1h 30m", "m", "h"])
def test_invalid_durations(catalog_data, value):
    task_of(catalog_data)["duration"] = value
    assert types(catalog_data) & {"string_pattern_mismatch", "string_too_short"}


@pytest.mark.parametrize("value", ["1.0.0", "0.1.0", "4.0.0-rc.1", "1.2.3+build.5", "10.20.30"])
def test_valid_versions(catalog_data, value):
    catalog_data["workbook"]["version"] = value
    assert errors_for(catalog_data) == []


@pytest.mark.parametrize("value", ["1.0", "v1.0.0", "01.0.0", "1.0.0.0", "latest", ""])
def test_invalid_versions(catalog_data, value):
    catalog_data["workbook"]["version"] = value
    assert "string_pattern_mismatch" in types(catalog_data)


# ------------------------------------------------------------------ asset paths


@pytest.mark.parametrize("src", ["assets/a.png", "assets/demo/topology.png", "assets/./demo/a.png"])
def test_safe_asset_paths(src):
    assert is_safe_asset_path(src)


@pytest.mark.parametrize(
    "src",
    [
        "../secret.png",
        "assets/../users.yaml",
        "assets/../../etc/passwd",
        "/etc/passwd",
        "images/a.png",
        "assets",
        "assets/",
        "assets\\..\\x.png",
        "assets/%2e%2e/x.png",
        "https://example.com/a.png",
        "C:/a.png",
        "",
    ],
)
def test_unsafe_asset_paths(src, catalog_data):
    assert not is_safe_asset_path(src)
    if src:
        task = task_of(catalog_data)
        task.pop("requirement")
        task["blocks"] = [{"type": "image", "src": src, "alt": "x"}]
        assert "asset_path" in types(catalog_data)
