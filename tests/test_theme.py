from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.models.catalog import Catalog, Stylesheet
from app.renderer import STATIC_DIR, render_static
from app.theme import theme_css, theme_hash

FULL = {
    "paper": "#F7F5F0",
    "card": "#FFFFFF",
    "ink": "#111111",
    "muted": "#555",
    "line": "#DDDDDD",
    "accent": "#0F4C3A",
    "accent_hover": "#0A3A2C",
    "accent_soft": "#E5EEEA",
    "ok": "#1F7A4D",
    "redo": "#B5541C",
    "hint_bg": "#EEF3F9",
    "trainer_bg": "#FAF5EA",
    "bonus_bg": "#F3F4F7",
    "level_palette": {
        "blue": "#2B5C8A",
        "ochre": "#9A6A12",
        "green": "#2E7D52",
        "grey": "#5B6878",
        "red": "#A8432A",
    },
}


def test_no_stylesheet_gives_empty_css():
    assert theme_css(None) == ""
    assert theme_css(Stylesheet()) == ""


def test_partial_stylesheet_contains_only_set_keys():
    css = theme_css(
        Stylesheet.model_validate({"accent": "#0F4C3A", "level_palette": {"blue": "#2B5C8A"}})
    )
    assert css == ":root {\n  --accent: #0F4C3A;\n  --level-blue: #2B5C8A;\n}\n"


def test_full_stylesheet_maps_every_key():
    css = theme_css(Stylesheet.model_validate(FULL))
    props = re.findall(r"(--[a-z-]+):", css)
    assert props == [
        "--paper", "--card", "--ink", "--muted", "--line", "--accent", "--accent-hover",
        "--accent-soft", "--ok", "--redo", "--hint-bg", "--trainer-bg", "--bonus-bg",
        "--level-blue", "--level-ochre", "--level-green", "--level-grey", "--level-red",
    ]  # fmt: skip
    assert css.count("{") == 1 and css.startswith(":root {")


def test_every_theme_property_exists_in_tokens():
    tokens = (STATIC_DIR / "css" / "tokens.css").read_text(encoding="utf-8")
    for prop in re.findall(r"(--[a-z-]+):", theme_css(Stylesheet.model_validate(FULL))):
        assert f"{prop}:" in tokens


@pytest.mark.parametrize(
    "value",
    [
        "red",
        "#12345",
        "#1234567",
        "#GGGGGG",
        "123456",
        "#123456; } body { display:none",
        "#123456\n",
        "rgb(1,2,3)",
        "var(--ink)",
        "",
    ],
)
def test_invalid_colors_rejected(value):
    with pytest.raises(ValidationError):
        Stylesheet.model_validate({"accent": value})
    with pytest.raises(ValidationError):
        Stylesheet.model_validate({"level_palette": {"blue": value}})


def test_unknown_stylesheet_keys_rejected():
    with pytest.raises(ValidationError):
        Stylesheet.model_validate({"background": "#FFFFFF"})
    with pytest.raises(ValidationError):
        Stylesheet.model_validate({"level_palette": {"purple": "#FFFFFF"}})


def test_unquoted_yaml_color_null_is_rejected():
    # `accent: #0F4C3A` in YAML is a comment -> null.
    with pytest.raises(ValidationError, match="must be quoted"):
        Stylesheet.model_validate({"accent": None})


def test_non_string_color_rejected():
    with pytest.raises(ValidationError):
        Stylesheet.model_validate({"accent": 123456})


def test_theme_hash_changes_with_content():
    a = theme_css(Stylesheet.model_validate({"accent": "#0F4C3A"}))
    b = theme_css(Stylesheet.model_validate({"accent": "#0F4C3B"}))
    assert theme_hash(a) != theme_hash(b)
    assert theme_hash(a) == theme_hash(a)
    assert len(theme_hash("")) == 16


def _render(data, stylesheet):
    data = {**data, "workbook": {**data["workbook"]}}
    if stylesheet is not None:
        data["workbook"]["stylesheet"] = stylesheet
    return render_static(Catalog.model_validate(data))


def test_partial_stylesheet_changes_only_overridden_colors(catalog_data):
    plain = _render(catalog_data, None)
    themed = _render(catalog_data, {"accent": "#0F4C3A", "paper": "#F7F5F0"})
    assert "#0F4C3A" not in plain
    # The only difference is the inlined theme block, which contains exactly the overrides.
    blocks = list(re.finditer(r"<style>\s*(.*?)\s*</style>\s*", themed, flags=re.S))
    assert len(blocks) == 2
    assert blocks[1].group(1) == ":root {\n  --paper: #F7F5F0;\n  --accent: #0F4C3A;\n}"
    without_theme = themed[: blocks[1].start()] + themed[blocks[1].end() :]
    assert without_theme == plain


def test_template_without_stylesheet_has_no_theme_block(catalog_data):
    html = _render(catalog_data, None)
    assert len(re.findall(r"<style>", html)) == 1
