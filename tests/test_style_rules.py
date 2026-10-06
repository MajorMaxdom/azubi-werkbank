"""Guards for the design and language rules in CLAUDE.md."""

from __future__ import annotations

import re

import pytest

from app.i18n import Translator, get_translator
from app.renderer import STATIC_DIR, TEMPLATES_DIR

CSS_FILES = sorted((STATIC_DIR / "css").glob("*.css"))
TEMPLATES = sorted(TEMPLATES_DIR.rglob("*.html"))


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.mark.parametrize("path", [p for p in CSS_FILES if p.name != "tokens.css"], ids=str)
def test_no_literal_colors_outside_tokens(path):
    css = strip_comments(path.read_text(encoding="utf-8"))
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\b(rgba?|hsla?)\(", css)


@pytest.mark.parametrize("path", CSS_FILES, ids=str)
def test_no_shadows_gradients_or_large_radius(path):
    css = strip_comments(path.read_text(encoding="utf-8"))
    assert "box-shadow" not in css
    assert "gradient" not in css
    for value in re.findall(r"border-radius:\s*([^;]+);", css):
        if value.strip() == "var(--radius)":
            continue
        assert all(int(px) <= 4 for px in re.findall(r"(\d+)px", value)), value
        assert "%" not in value and "999" not in value
    for value in re.findall(r"--radius:\s*(\d+)px", css):
        assert int(value) <= 4


def test_tokens_match_claude_md_defaults():
    tokens = (STATIC_DIR / "css" / "tokens.css").read_text(encoding="utf-8")
    expected = {
        "--paper": "#F4F6F8", "--card": "#FFFFFF", "--ink": "#1A2433", "--muted": "#5B6878",
        "--line": "#D6DCE3", "--accent": "#1B365D", "--ok": "#1F7A4D", "--redo": "#B5541C",
        "--hint-bg": "#EEF3F9", "--trainer-bg": "#FAF5EA", "--bonus-bg": "#F3F4F7",
        "--level-blue": "#2F5D8A", "--level-ochre": "#9A6A12", "--level-green": "#2E7D52",
        "--level-grey": "#5B6878", "--level-red": "#A8432A",
    }  # fmt: skip
    for prop, value in expected.items():
        assert f"{prop}: {value};" in tokens
    assert "--accent-hover: color-mix(in srgb, var(--accent) 75%, black);" in tokens
    assert "--accent-soft: color-mix(in srgb, var(--accent) 10%, white);" in tokens


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_templates_have_no_inline_style_or_script(path):
    html = path.read_text(encoding="utf-8")
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert "<script" not in html


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_templates_have_no_hardcoded_text(path):
    html = path.read_text(encoding="utf-8")
    text = re.sub(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", " ", html, flags=re.S)
    text = re.sub(r"<!DOCTYPE[^>]*>|<[^>]+>", " ", text)
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", text)
    assert words == [], f"hard-coded text in {path.name}: {words}"


def test_all_template_translation_keys_exist():
    t = get_translator()
    keys = set()
    for path in TEMPLATES:
        keys |= set(
            re.findall(r"""\bt\(\s*['"]([a-z0-9_.]+)['"]""", path.read_text(encoding="utf-8"))
        )
    assert keys
    for key in keys:
        t.lookup(key)


def test_translator_plural_and_missing():
    t = Translator({"x": {"one": "{count} Ding", "other": "{count} Dinge"}, "y": "Hallo {name}"})
    assert t("x", count=1) == "1 Ding"
    assert t("x", count=3) == "3 Dinge"
    assert t("y", name="Welt") == "Hallo Welt"
    with pytest.raises(KeyError):
        t("missing.key")
